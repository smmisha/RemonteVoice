// User-mode test substitutes, never used by the driver build.
#pragma once
#include "portcls.h"
inline uint32_t IoGetRequestorProcessId(void* irp) {
    return *static_cast<const uint32_t*>(irp);
}
