#include "fishhook.h"

#include <CommonCrypto/CommonKeyDerivation.h>
#include <dirent.h>
#include <dlfcn.h>
#include <fcntl.h>
#include <limits.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

typedef struct sqlite3 sqlite3;

struct db_salt {
  uint8_t kdf[16];
  uint8_t mac[16];
  char account[PATH_MAX];
};

static struct db_salt *salts;
static size_t salt_count;

static int (*original_sqlite3_key)(sqlite3 *, const void *, int);
static int (*original_sqlite3_key_v2)(sqlite3 *, const char *, const void *, int);
static int (*original_pbkdf)(CCPBKDFAlgorithm, const char *, size_t,
                             const uint8_t *, size_t,
                             CCPseudoRandomAlgorithm, uint32_t,
                             uint8_t *, size_t);

static void write_candidate(const char *account, const uint8_t *key,
                            size_t length) {
  const char *path = getenv("EMOJIFAV_KEY_OUT");
  if (!path || !*path || !key || length != 32) return;

  static const char hex[] = "0123456789abcdef";
  char encoded[65];
  for (size_t i = 0; i < length; i++) {
    encoded[i * 2] = hex[key[i] >> 4];
    encoded[i * 2 + 1] = hex[key[i] & 15];
  }
  encoded[64] = '\0';

  int fd = open(path, O_WRONLY | O_CREAT | O_APPEND, 0600);
  if (fd < 0) return;
  dprintf(fd, "%s %s\n", account && *account ? account : "*", encoded);
  close(fd);
}

static void write_sqlite_candidate(const void *key, int length) {
  if (!key || length <= 0) return;
  if (length == 32) {
    write_candidate("*", key, 32);
    return;
  }
  if (length != 64) return;

  const char *text = key;
  uint8_t decoded[32];
  for (size_t i = 0; i < 32; i++) {
    char pair[3] = {text[i * 2], text[i * 2 + 1], '\0'};
    char *end = NULL;
    unsigned long value = strtoul(pair, &end, 16);
    if (!end || *end) return;
    decoded[i] = (uint8_t)value;
  }
  write_candidate("*", decoded, sizeof(decoded));
}

static int hook_sqlite3_key(sqlite3 *db, const void *key, int length) {
  write_sqlite_candidate(key, length);
  return original_sqlite3_key ? original_sqlite3_key(db, key, length) : 0;
}

static int hook_sqlite3_key_v2(sqlite3 *db, const char *name,
                               const void *key, int length) {
  write_sqlite_candidate(key, length);
  return original_sqlite3_key_v2
             ? original_sqlite3_key_v2(db, name, key, length)
             : 0;
}

static const struct db_salt *matching_salt(const uint8_t *salt,
                                           bool mac_salt) {
  if (!salt) return NULL;
  for (size_t i = 0; i < salt_count; i++) {
    const uint8_t *expected = mac_salt ? salts[i].mac : salts[i].kdf;
    if (memcmp(salt, expected, 16) == 0) return &salts[i];
  }
  return NULL;
}

static int hook_pbkdf(CCPBKDFAlgorithm algorithm, const char *password,
                      size_t password_length, const uint8_t *salt,
                      size_t salt_length, CCPseudoRandomAlgorithm prf,
                      uint32_t rounds, uint8_t *derived,
                      size_t derived_length) {
  int result = original_pbkdf
                   ? original_pbkdf(algorithm, password, password_length, salt,
                                    salt_length, prf, rounds, derived,
                                    derived_length)
                   : -1;
  if (result != 0 || algorithm != kCCPBKDF2 || salt_length != 16) {
    return result;
  }

  const struct db_salt *entry = matching_salt(salt, false);
  if (entry && rounds > 2 && derived && derived_length >= 32) {
    write_candidate(entry->account, derived, 32);
  }

  entry = matching_salt(salt, true);
  if (entry && rounds <= 2 && password && password_length == 32) {
    write_candidate(entry->account, (const uint8_t *)password, 32);
  }
  return result;
}

static bool ignored_account(const char *name) {
  return !name || !*name || name[0] == '.' ||
         strcmp(name, "all_users") == 0 || strcmp(name, "Backup") == 0 ||
         strcmp(name, "WMPF") == 0 || strcmp(name, "AppletCaches") == 0 ||
         strcmp(name, "Update") == 0;
}

static void load_database_salts(void) {
  const char *root = getenv("EMOJIFAV_WECHAT_ROOT");
  if (!root || !*root) return;

  DIR *directory = opendir(root);
  if (!directory) return;
  struct dirent *item;
  while ((item = readdir(directory)) != NULL) {
    if (ignored_account(item->d_name)) continue;

    char path[PATH_MAX];
    snprintf(path, sizeof(path), "%s/%s/db_storage/emoticon/emoticon.db",
             root, item->d_name);
    int fd = open(path, O_RDONLY);
    if (fd < 0) continue;

    uint8_t salt[16];
    ssize_t bytes = read(fd, salt, sizeof(salt));
    close(fd);
    if (bytes != sizeof(salt)) continue;

    struct db_salt *expanded =
        realloc(salts, sizeof(*salts) * (salt_count + 1));
    if (!expanded) break;
    salts = expanded;
    memcpy(salts[salt_count].kdf, salt, 16);
    for (size_t i = 0; i < 16; i++) {
      salts[salt_count].mac[i] = salt[i] ^ 0x3a;
    }
    snprintf(salts[salt_count].account,
             sizeof(salts[salt_count].account), "%s", item->d_name);
    salt_count++;
  }
  closedir(directory);
}

__attribute__((constructor)) static void initialize(void) {
  if (!getenv("EMOJIFAV_KEY_OUT")) return;
  load_database_salts();
  struct rebinding hooks[] = {
      {"sqlite3_key", (void *)hook_sqlite3_key,
       (void **)&original_sqlite3_key},
      {"sqlite3_key_v2", (void *)hook_sqlite3_key_v2,
       (void **)&original_sqlite3_key_v2},
      {"CCKeyDerivationPBKDF", (void *)hook_pbkdf,
       (void **)&original_pbkdf},
  };
  rebind_symbols(hooks, sizeof(hooks) / sizeof(hooks[0]));
}
