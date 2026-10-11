"""Small trusted-target contract. No cloud discovery, automatic resize or price catalog."""
from datetime import datetime, timezone

SIZE_FIELDS = {'aws': 'instance_type', 'gcp': 'machine_type', 'azure': 'vm_size'}


def capabilities(provider, kind='app_cluster'):
    if (provider not in SIZE_FIELDS or kind not in ('app_cluster', 'database_cluster')
            or (kind == 'database_cluster' and provider not in ('aws', 'gcp'))):
        return {'provider_kind': provider, 'supported': False, 'actions': {}}
    return {'provider_kind': provider, 'supported': True, 'kind': kind,
            'verification': 'target_unverified', 'billing_reservation_supported': provider in {'aws', 'gcp', 'azure'},
            'actions': {'plan': True, 'apply_saved_plan': True, 'resize': 'maintenance_required',
                        'start': False, 'stop': False, 'delete': False, 'node_scale_out': False,
                        'automatic_resize': False, 'snapshot_restore': False}}


def validate_profile(target):
    provider = target.get('provider_kind')
    if provider not in SIZE_FIELDS:
        raise ValueError('provider capability unsupported')
    profile = target.get('profile')
    if (not isinstance(profile, dict) or set(profile) != {'kind', 'allowed_sizes'}
            or profile['kind'] not in ('app_cluster', 'database_cluster') or not isinstance(profile['allowed_sizes'], list)
            or not profile['allowed_sizes'] or not all(isinstance(x, str) and x for x in profile['allowed_sizes'])):
        raise ValueError('registered app_cluster or database_cluster size profile required')
    supported = capabilities(provider, profile['kind'])
    purpose = 'database' if profile['kind'] == 'database_cluster' else 'runtime'
    if not supported['supported'] or target['variables'].get('purpose', 'runtime') != purpose:
        raise ValueError('profile kind and supported node purpose must match')
    selected = target['variables'].get(SIZE_FIELDS[provider])
    if selected not in profile['allowed_sizes']:
        raise ValueError('explicit size must belong to the registered target profile')
    return supported


def timestamp(value):
    value = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if value.tzinfo is None:
        raise ValueError('UTC observation time required')
    return value.astimezone(timezone.utc)


def validate_maintenance(value, receipt, *, now=None):
    """Trusted caller's fresh drain observation, not a user-supplied approval token."""
    now = now or datetime.now(timezone.utc)
    if not isinstance(value, dict):
        raise ValueError('fresh maintenance observation required')
    for key in ('target_id', 'owner_ref', 'plan_sha256'):
        if value.get(key) != receipt[key]:
            raise ValueError('maintenance observation target/plan mismatch')
    age = (now - timestamp(value.get('observed_at', ''))).total_seconds()
    if not 0 <= age <= 600:
        raise ValueError('maintenance observation stale')
    if any(value.get(key) is not True for key in ('dispatch_blocked', 'dependencies_stopped', 'backup_verified')):
        raise ValueError('drain, dependency stop and backup verification required')
    if any(type(value.get(key)) is not int or value[key] != 0 for key in ('active_jobs', 'active_leases')):
        raise ValueError('active jobs or desktop leases prevent maintenance')
