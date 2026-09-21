# common role

This role is intentionally audit-only until the lab's desired state is defined. It currently verifies the platform and reports the discovered host identity; it does not install packages, enable services, modify files, manage users, or change networking.

Add changes as small, reviewable tasks and keep secrets in Ansible Vault or an external secret manager.
