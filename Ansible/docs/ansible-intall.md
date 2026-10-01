# Ansible installation

This document records the user-local Ansible installation used for TheGarden.
The exact Python and Ansible versions may vary by host; the repository does not
require a system-wide Ansible installation.

## Recommended installation

Use the host's supported Python environment or a user-owned virtual
environment. From the repository:

```bash
cd TheGarden/Ansible
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install ansible-core
.venv/bin/ansible-galaxy collection install -r requirements.yml
```

Alternatively, keep the virtual environment in a user-local path and add its
`bin` directory to `PATH`:

```bash
export PATH="$HOME/.local/bin:$PATH"
```

Do not modify the system Python installation just to run this project.

## Verify the installation

```bash
cd TheGarden/Ansible
ansible --version
ansible-config dump --only-changed
ansible-inventory --graph
ansible localhost -c local -m ansible.builtin.ping
```

The default configuration is loaded only when commands are run from the
`Ansible/` project directory or when its configuration is selected explicitly.

## Collections

`ansible-core` does not include every community collection. Install the
repository requirements before running Docker deployment playbooks:

```bash
cd TheGarden/Ansible
ansible-galaxy collection install -r requirements.yml
```

The Docker playbook uses the `community.docker.docker_compose_v2` module.

## SSH and worker hosts

Ansible uses the SSH account and authentication method configured in
`inventory/hosts.yml`. It does not require a separate permission grant for the
Ansible executable.

Test one worker before targeting the group:

```bash
cd TheGarden/Ansible
ansible agent_workers --limit inkii -m ansible.builtin.ping -vvv
```

If manual SSH uses a password, use `--ask-pass` for testing. For unattended
worker provisioning, use a normal SSH key or a working Tailscale SSH policy.
Do not store passwords or private keys in inventory files.

## Validation commands

```bash
cd TheGarden/Ansible
ansible-playbook playbooks/site.yml --syntax-check
ansible-playbook playbooks/audit.yml --syntax-check
ansible-playbook playbooks/proxmox-audit.yml --syntax-check
ansible-playbook playbooks/docker-services.yml --syntax-check
ansible-playbook playbooks/agent-workers.yml --syntax-check
```

The worker role is being developed as a controlled provisioning path. Until it
is complete, use `TheGarden/agent-worker/README.md` for the manual worker
runtime setup and troubleshooting procedure.
