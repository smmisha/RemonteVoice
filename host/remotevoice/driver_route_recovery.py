"""Durable restore-only route IDs; never admission or backend readiness policy.

The existing Host route owner may use this record under its exclusive Host lease
to restore still-managed defaults even when broker startup is unavailable. This
module never activates endpoints, changes Windows devices or removes the record.
"""
from __future__ import annotations

import json
import os
import secrets
import unicodedata
from dataclasses import dataclass
from pathlib import Path

_NAME = 'driver-route-recovery.json'
_LIMIT = 16 * 1024
_FIELDS = ('output_remote', 'output_fallback', 'input_remote', 'input_fallback')


class DriverRecoveryError(RuntimeError):
    """Restore-only record could not be read/written; no local IDs in errors."""


def _endpoint(value):
    if (type(value) is not str or not value.strip() or value != value.strip() or len(value) > 1024
            or any(unicodedata.category(character) in ('Cc', 'Cs') for character in value)):
        raise DriverRecoveryError('Invalid driver route recovery record')


@dataclass(frozen=True, repr=False)
class DriverRouteRecovery:
    output_remote: str
    output_fallback: str
    input_remote: str
    input_fallback: str

    def __post_init__(self):
        for field in _FIELDS:
            _endpoint(getattr(self, field))
        if (self.output_remote.casefold() == self.output_fallback.casefold()
                or self.input_remote.casefold() == self.input_fallback.casefold()):
            raise DriverRecoveryError('Invalid driver route recovery record')


def _pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise DriverRecoveryError('Invalid driver route recovery record')
        result[key] = value
    return result


def load_driver_recovery(data_dir: Path) -> DriverRouteRecovery | None:
    """Bounded strict decode; absence is allowed, malformed data fails closed."""
    target = Path(data_dir) / _NAME
    try:
        with target.open('rb') as stream:
            data = stream.read(_LIMIT + 1)
        if len(data) > _LIMIT:
            raise DriverRecoveryError('Invalid driver route recovery record')
        document = json.loads(data.decode('utf-8'), object_pairs_hook=_pairs)
        if (type(document) is not dict or set(document) != {'schema_version', *_FIELDS}
                or type(document['schema_version']) is not int or document['schema_version'] != 1):
            raise DriverRecoveryError('Invalid driver route recovery record')
        return DriverRouteRecovery(*(document[name] for name in _FIELDS))
    except FileNotFoundError:
        return None
    except (OSError, ValueError, TypeError, RecursionError, DriverRecoveryError):
        raise DriverRecoveryError('Driver route recovery record is unavailable') from None


def save_driver_recovery(data_dir: Path, plan: DriverRouteRecovery) -> None:
    """Flush and atomically replace a fixed record in the same directory."""
    if type(plan) is not DriverRouteRecovery:
        raise DriverRecoveryError('Invalid driver route recovery record')
    folder = Path(data_dir)
    target = folder / _NAME
    temporary = folder / ('.driver-route-recovery-' + secrets.token_hex(12) + '.tmp')
    document = {'schema_version': 1, **{name: getattr(plan, name) for name in _FIELDS}}
    payload = (json.dumps(document, ensure_ascii=True, separators=(',', ':')) + '\n').encode('ascii')
    if len(payload) > _LIMIT:
        raise DriverRecoveryError('Invalid driver route recovery record')
    created = False
    try:
        folder.mkdir(parents=True, exist_ok=True)
        with temporary.open('xb') as stream:
            created = True
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    except (OSError, ValueError, TypeError):
        raise DriverRecoveryError('Driver route recovery record could not be saved') from None
    finally:
        try:
            if created:
                temporary.unlink(missing_ok=True)
        except OSError:
            raise DriverRecoveryError('Driver route recovery temporary cleanup is unconfirmed') from None
