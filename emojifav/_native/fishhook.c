// Copyright (c) 2013, Facebook, Inc.
// All rights reserved.
// Redistribution and use in source and binary forms, with or without
// modification, are permitted provided that the following conditions are met:
//   * Redistributions of source code must retain the above copyright notice,
//     this list of conditions and the following disclaimer.
//   * Redistributions in binary form must reproduce the above copyright notice,
//     this list of conditions and the following disclaimer in the documentation
//     and/or other materials provided with the distribution.
//   * Neither the name Facebook nor the names of its contributors may be used to
//     endorse or promote products derived from this software without specific
//     prior written permission.
// THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
// AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
// IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE
// DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE LIABLE
// FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL
// DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR
// SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER
// CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY,
// OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE
// OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.

#include "fishhook.h"

#include <dlfcn.h>
#include <mach/mach.h>
#include <mach-o/dyld.h>
#include <mach-o/loader.h>
#include <mach-o/nlist.h>
#include <stdbool.h>
#include <stdlib.h>
#include <string.h>

#ifdef __LP64__
typedef struct mach_header_64 mach_header_t;
typedef struct segment_command_64 segment_command_t;
typedef struct section_64 section_t;
typedef struct nlist_64 nlist_t;
#define LC_SEGMENT_ARCH_DEPENDENT LC_SEGMENT_64
#else
typedef struct mach_header mach_header_t;
typedef struct segment_command segment_command_t;
typedef struct section section_t;
typedef struct nlist nlist_t;
#define LC_SEGMENT_ARCH_DEPENDENT LC_SEGMENT
#endif

#ifndef SEG_DATA_CONST
#define SEG_DATA_CONST "__DATA_CONST"
#endif

struct rebindings_entry {
  struct rebinding *rebindings;
  size_t rebindings_nel;
  struct rebindings_entry *next;
};

static struct rebindings_entry *rebindings_head;

static int prepend_rebindings(struct rebindings_entry **head,
                              struct rebinding rebindings[], size_t count) {
  struct rebindings_entry *entry = malloc(sizeof(*entry));
  if (!entry) return -1;
  entry->rebindings = malloc(sizeof(struct rebinding) * count);
  if (!entry->rebindings) {
    free(entry);
    return -1;
  }
  memcpy(entry->rebindings, rebindings, sizeof(struct rebinding) * count);
  entry->rebindings_nel = count;
  entry->next = *head;
  *head = entry;
  return 0;
}

static void rebind_section(struct rebindings_entry *entries, section_t *section,
                           intptr_t slide, nlist_t *symtab, char *strtab,
                           uint32_t *indirect_symtab) {
  uint32_t *indices = indirect_symtab + section->reserved1;
  void **bindings = (void **)((uintptr_t)slide + section->addr);
  size_t count = section->size / sizeof(void *);

  for (size_t i = 0; i < count; i++) {
    uint32_t index = indices[i];
    if (index == INDIRECT_SYMBOL_ABS || index == INDIRECT_SYMBOL_LOCAL ||
        index == (INDIRECT_SYMBOL_ABS | INDIRECT_SYMBOL_LOCAL)) {
      continue;
    }
    char *name = strtab + symtab[index].n_un.n_strx;
    if (name[0] != '_') continue;
    name++;

    for (struct rebindings_entry *entry = entries; entry; entry = entry->next) {
      for (size_t j = 0; j < entry->rebindings_nel; j++) {
        if (strcmp(name, entry->rebindings[j].name) != 0) continue;
        if (entry->rebindings[j].replaced &&
            bindings[i] != entry->rebindings[j].replacement) {
          *entry->rebindings[j].replaced = bindings[i];
        }
        kern_return_t result = vm_protect(
            mach_task_self(), (vm_address_t)bindings,
            (vm_size_t)section->size, false,
            VM_PROT_READ | VM_PROT_WRITE | VM_PROT_COPY);
        if (result == KERN_SUCCESS) {
          bindings[i] = entry->rebindings[j].replacement;
        }
        goto next_symbol;
      }
    }
  next_symbol:;
  }
}

static void rebind_image(struct rebindings_entry *entries,
                         const struct mach_header *header, intptr_t slide) {
  Dl_info info;
  if (dladdr(header, &info) == 0) return;

  segment_command_t *segment = NULL;
  segment_command_t *linkedit = NULL;
  struct symtab_command *symtab_command = NULL;
  struct dysymtab_command *dysymtab_command = NULL;
  uintptr_t cursor = (uintptr_t)header + sizeof(mach_header_t);

  for (uint32_t i = 0; i < header->ncmds; i++) {
    segment = (segment_command_t *)cursor;
    if (segment->cmd == LC_SEGMENT_ARCH_DEPENDENT &&
        strcmp(segment->segname, SEG_LINKEDIT) == 0) {
      linkedit = segment;
    } else if (segment->cmd == LC_SYMTAB) {
      symtab_command = (struct symtab_command *)segment;
    } else if (segment->cmd == LC_DYSYMTAB) {
      dysymtab_command = (struct dysymtab_command *)segment;
    }
    cursor += segment->cmdsize;
  }
  if (!linkedit || !symtab_command || !dysymtab_command ||
      !dysymtab_command->nindirectsyms) return;

  uintptr_t base = (uintptr_t)slide + linkedit->vmaddr - linkedit->fileoff;
  nlist_t *symtab = (nlist_t *)(base + symtab_command->symoff);
  char *strtab = (char *)(base + symtab_command->stroff);
  uint32_t *indirect = (uint32_t *)(base + dysymtab_command->indirectsymoff);

  cursor = (uintptr_t)header + sizeof(mach_header_t);
  for (uint32_t i = 0; i < header->ncmds; i++) {
    segment = (segment_command_t *)cursor;
    if (segment->cmd == LC_SEGMENT_ARCH_DEPENDENT &&
        (strcmp(segment->segname, SEG_DATA) == 0 ||
         strcmp(segment->segname, SEG_DATA_CONST) == 0)) {
      for (uint32_t j = 0; j < segment->nsects; j++) {
        section_t *section =
            (section_t *)(cursor + sizeof(segment_command_t)) + j;
        uint32_t type = section->flags & SECTION_TYPE;
        if (type == S_LAZY_SYMBOL_POINTERS ||
            type == S_NON_LAZY_SYMBOL_POINTERS) {
          rebind_section(entries, section, slide, symtab, strtab, indirect);
        }
      }
    }
    cursor += segment->cmdsize;
  }
}

static void rebind_image_callback(const struct mach_header *header,
                                  intptr_t slide) {
  rebind_image(rebindings_head, header, slide);
}

int rebind_symbols(struct rebinding rebindings[], size_t count) {
  int result = prepend_rebindings(&rebindings_head, rebindings, count);
  if (result < 0) return result;
  if (!rebindings_head->next) {
    _dyld_register_func_for_add_image(rebind_image_callback);
  } else {
    uint32_t images = _dyld_image_count();
    for (uint32_t i = 0; i < images; i++) {
      rebind_image_callback(_dyld_get_image_header(i),
                            _dyld_get_image_vmaddr_slide(i));
    }
  }
  return 0;
}
