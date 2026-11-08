#pragma once
#include <portcls.h>
#include "rv_mic_protocol.h"

// {EE47BFAA-9650-41D5-AFA0-09CE9F32E71B}
static const GUID KSPROPSETID_RemoteVoiceMic =
    {0xee47bfaa, 0x9650, 0x41d5, {0xaf, 0xa0, 0x09, 0xce, 0x9f, 0x32, 0xe7, 0x1b}};

class RvPcmBridge {
    KSPIN_LOCK lock;
    HANDLE owner;
    ULONGLONG session, last_request, last_frame;
    UCHAR nonce[16];
    UCHAR buffer[9600];  // 100 ms, fixed nonpaged storage owned by the adapter.
    ULONG read_offset, write_offset, queued;
    void ClearQueue();
    void ClearOwner();
public:
    void Initialize();
    void Reset();
    NTSTATUS Property(PPCPROPERTY_REQUEST request);
    void Read(PUCHAR output, ULONG size);
};
