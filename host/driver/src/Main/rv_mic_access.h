#pragma once
#include <ntifs.h>
#include <portcls.h>

// The audio PDO is shared with the Windows audio engine. Only the private PCM
// property has this narrower policy; session/nonce validation remains in the bridge.
_IRQL_requires_(PASSIVE_LEVEL)
inline NTSTATUS RvCheckMicPropertyAccess(_In_ PPCPROPERTY_REQUEST request)
{
    if (KeGetCurrentIrql() != PASSIVE_LEVEL || !request || !request->Irp)
        return STATUS_INVALID_PARAMETER;

    // Capture the originating subject, never a worker's SYSTEM token. The private
    // synchronous property fails closed if PortCls changes execution context.
    if (request->Irp->Tail.Overlay.Thread != PsGetCurrentThread() ||
        IoGetRequestorProcess(request->Irp) != PsGetCurrentProcess())
        return STATUS_ACCESS_DENIED;

    SID_IDENTIFIER_AUTHORITY authority = SECURITY_NT_AUTHORITY;
    SID systemSid = {};
    struct { SID sid; ULONG secondSubAuthority; } administratorsSid = {};
    NTSTATUS status = RtlInitializeSid(&systemSid, &authority, 1);
    if (!NT_SUCCESS(status)) return status;
    *RtlSubAuthoritySid(&systemSid, 0) = SECURITY_LOCAL_SYSTEM_RID;
    status = RtlInitializeSid(&administratorsSid, &authority, 2);
    if (!NT_SUCCESS(status)) return status;
    *RtlSubAuthoritySid(&administratorsSid, 0) = SECURITY_BUILTIN_DOMAIN_RID;
    *RtlSubAuthoritySid(&administratorsSid, 1) = DOMAIN_ALIAS_RID_ADMINS;

    // The union gives the stack ACL its required alignment and space for two ACEs.
    union {
        ULONG alignment;
        UCHAR bytes[sizeof(ACL) + 2 * (sizeof(ACCESS_ALLOWED_ACE) + SECURITY_MAX_SID_SIZE)];
    } aclStorage = {};
    PACL acl = reinterpret_cast<PACL>(aclStorage.bytes);
    status = RtlCreateAcl(acl, sizeof(aclStorage.bytes), ACL_REVISION);
    if (!NT_SUCCESS(status)) return status;
    status = RtlAddAccessAllowedAce(acl, ACL_REVISION, FILE_WRITE_DATA, &systemSid);
    if (!NT_SUCCESS(status)) return status;
    status = RtlAddAccessAllowedAce(acl, ACL_REVISION, FILE_WRITE_DATA, &administratorsSid);
    if (!NT_SUCCESS(status)) return status;

    SECURITY_DESCRIPTOR descriptor = {};
    status = RtlCreateSecurityDescriptor(&descriptor, SECURITY_DESCRIPTOR_REVISION);
    if (!NT_SUCCESS(status)) return status;
    status = RtlSetOwnerSecurityDescriptor(&descriptor, &systemSid, FALSE);
    if (!NT_SUCCESS(status)) return status;
    status = RtlSetGroupSecurityDescriptor(&descriptor, &administratorsSid, FALSE);
    if (!NT_SUCCESS(status)) return status;
    status = RtlSetDaclSecurityDescriptor(&descriptor, TRUE, acl, FALSE);
    if (!NT_SUCCESS(status)) return status;

    GENERIC_MAPPING mapping = {FILE_GENERIC_READ, FILE_GENERIC_WRITE, FILE_GENERIC_EXECUTE, FILE_ALL_ACCESS};
    SECURITY_SUBJECT_CONTEXT subject = {};
    ACCESS_MASK granted = 0;
    NTSTATUS accessStatus = STATUS_ACCESS_DENIED;
    SeCaptureSubjectContext(&subject);
    // UserMode is deliberate even for kernel-originated IRPs: no mode bypass.
    // SeAccessCheck handles disabled/deny-only groups and restricted tokens.
    BOOLEAN allowed = SeAccessCheck(&descriptor, &subject, FALSE, FILE_WRITE_DATA,
        0, nullptr, &mapping, UserMode, &granted, &accessStatus);
    SeReleaseSubjectContext(&subject);
    return allowed ? STATUS_SUCCESS : accessStatus;
}
