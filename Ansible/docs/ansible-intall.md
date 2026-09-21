# Lightweight Ansible Installation

**Status:** Installed and verified successfully  
**Recorded:** 2026-09-21 09:12:45 -0400  
**Account:** `df-server` (`uid=1000`)  
**Host:** `server-debian`

## Summary

The lightweight Ansible distribution, **`ansible-core`**, was installed for the current user. `ansible-core` provides the Ansible command-line tools and built-in functionality without installing the larger `ansible` community bundle and its collection set.

The installation is user-local and isolated in a Python virtual environment because this account did not have a usable non-interactive `sudo` session (`sudo` requested a password). No system package database or system Python installation was modified.

| Item | Value |
| --- | --- |
| Ansible package | `ansible-core` |
| Ansible version | `2.21.4` |
| Python used by Ansible | `3.13.5` |
| Virtual environment | `/home/df-server/.local/share/ansible-core-venv` |
| CLI symlink directory | `/home/df-server/.local/bin` |
| Installation footprint at completion | approximately `57M` |
| Installation source | PyPI over HTTPS |

## Host and pre-installation checks

The machine was identified as:

- Debian GNU/Linux 13.7 (`trixie`), `amd64`
- Kernel: `6.12.107+deb13-amd64`
- System Python: `/usr/bin/python3` (`3.13.5`)
- Available system package manager: `/usr/bin/apt-get`
- Ansible was not installed before this work (`ansible` was not on `PATH`)
- Python `pip` was not installed before this work

Debian's package index contained the following candidates:

- `ansible-core`: `2.19.11-0+deb13u1`
- `ansible`: `12.0.0+dfsg-0+deb13u1`

The Debian package route was not used because `sudo -n` returned `sudo: a password is required`. A temporary `ansible-core` Debian archive was downloaded with the unprivileged `apt-get download` command as a package-availability check and then removed; it was not installed or extracted into the system.

## Installation method

The following approach was used:

1. Create a user-owned virtual environment without relying on Debian's unavailable `ensurepip` module.
2. Download the official PyPA `get-pip.py` bootstrap script over HTTPS into a temporary file.
3. Install `pip` inside the virtual environment only.
4. Install the pinned `ansible-core==2.21.4` package and its Python dependencies from PyPI.
5. Create user-local symlinks in `~/.local/bin` for the Ansible command-line tools.
6. Verify the installation with version, module, and login-shell checks.

The effective commands were:

```bash
VENV="$HOME/.local/share/ansible-core-venv"
mkdir -p "$HOME/.local/share"
python3 -m venv --without-pip "$VENV"

BOOTSTRAP="$(mktemp)"
curl -fsSL --retry 3 --max-time 60 \
  https://bootstrap.pypa.io/get-pip.py -o "$BOOTSTRAP"
"$VENV/bin/python" "$BOOTSTRAP" --disable-pip-version-check
rm -f "$BOOTSTRAP"

"$VENV/bin/python" -m pip install --disable-pip-version-check \
  --no-cache-dir 'ansible-core==2.21.4'

mkdir -p "$HOME/.local/bin"
for command in \
  ansible ansible-config ansible-console ansible-doc ansible-galaxy \
  ansible-inventory ansible-playbook ansible-pull ansible-test ansible-vault
do
  ln -s "$VENV/bin/$command" "$HOME/.local/bin/$command"
done
```

The virtual environment was newly created at installation time; an existing directory at that path was not overwritten.

## Installed files

The virtual environment contains the Python interpreter, `pip`, Ansible, and its isolated dependencies:

```text
/home/df-server/.local/share/ansible-core-venv/
```

The following user-local commands now point into that environment:

```text
/home/df-server/.local/bin/ansible
/home/df-server/.local/bin/ansible-config
/home/df-server/.local/bin/ansible-console
/home/df-server/.local/bin/ansible-doc
/home/df-server/.local/bin/ansible-galaxy
/home/df-server/.local/bin/ansible-inventory
/home/df-server/.local/bin/ansible-playbook
/home/df-server/.local/bin/ansible-pull
/home/df-server/.local/bin/ansible-test
/home/df-server/.local/bin/ansible-vault
```

`~/.local/bin` was already included by the existing `~/.profile` for login shells. No shell startup file was changed during this installation. A non-login shell that does not inherit that path can use:

```bash
export PATH="$HOME/.local/bin:$PATH"
```

## Installed Python packages

The final virtual-environment package inventory was:

```text
ansible-core==2.21.4
cffi==2.1.1
cryptography==50.0.1
Jinja2==3.1.6
MarkupSafe==3.0.3
packaging==26.3
pip==26.2.1
pycparser==3.0
PyYAML==6.0.3
resolvelib==1.2.1
```

Only the runtime dependencies required by `ansible-core` were installed. The full `ansible` community bundle, optional collections, and optional integration packages were not installed.

## Verification performed

### Version and executable checks

The installed tools reported:

```text
ansible [core 2.21.4]
ansible-playbook [core 2.21.4]
ansible-galaxy [core 2.21.4]
```

An Ansible version check also confirmed:

```text
ansible python module location = /home/df-server/.local/share/ansible-core-venv/lib/python3.13/site-packages/ansible
executable location = /home/df-server/.local/bin/ansible
python version = 3.13.5
jinja version = 3.1.6
pyyaml version = 6.0.3
```

All ten expected command names listed above resolve from `~/.local/bin`.

### Functional local-module check

The built-in ping module succeeded against the implicit local host:

```bash
PATH="$HOME/.local/bin:$PATH" \
  ansible localhost -c local -m ansible.builtin.ping
```

Result:

```text
localhost | SUCCESS => {
    "changed": false,
    "ping": "pong"
}
```

A setup-module check also reported the expected target Python version:

```text
ansible_python_version: 3.13.5
```

The test emitted the expected warning that no inventory file was configured and that only implicit localhost was available. No remote hosts or production systems were contacted.

### Login-shell check

A fresh login-shell test resolved Ansible without an extra PATH change:

```text
/home/df-server/.local/bin/ansible
ansible [core 2.21.4]
```

## Normal usage

For a login shell, use Ansible directly:

```bash
ansible --version
ansible localhost -c local -m ansible.builtin.ping
```

For the current non-login shell, prepend the user-local directory if necessary:

```bash
export PATH="$HOME/.local/bin:$PATH"
```

The virtual environment can also be activated explicitly:

```bash
source "$HOME/.local/share/ansible-core-venv/bin/activate"
```

To run a playbook, provide an inventory and playbook in the usual way:

```bash
ansible-playbook -i inventory.ini site.yml
```

To add a community collection later (which is separate from `ansible-core`), use for example:

```bash
ansible-galaxy collection install community.general
```

No collection was installed as part of this lightweight setup.

## Maintenance

Show the installed package versions:

```bash
"$HOME/.local/share/ansible-core-venv/bin/python" -m pip freeze
```

Upgrade to a specific tested version:

```bash
VENV="$HOME/.local/share/ansible-core-venv"
"$VENV/bin/python" -m pip install --upgrade 'ansible-core==2.21.4'
```

To deliberately move to a newer release, replace the version pin, then rerun the verification commands and update this document with the new package inventory.

To remove this user-local installation, first remove only the symlinks listed in the **Installed files** section and then remove the virtual environment:

```bash
rm -f "$HOME/.local/bin/ansible" \
      "$HOME/.local/bin/ansible-config" \
      "$HOME/.local/bin/ansible-console" \
      "$HOME/.local/bin/ansible-doc" \
      "$HOME/.local/bin/ansible-galaxy" \
      "$HOME/.local/bin/ansible-inventory" \
      "$HOME/.local/bin/ansible-playbook" \
      "$HOME/.local/bin/ansible-pull" \
      "$HOME/.local/bin/ansible-test" \
      "$HOME/.local/bin/ansible-vault"
rm -rf "$HOME/.local/share/ansible-core-venv"
```

This does not remove any system packages or collections that may be installed separately in the future.

## Scope and limitations

- This is a **current-user** installation. It is not available to other accounts unless they perform their own installation or PATH setup.
- It is not a system-wide `apt` installation. If a system-wide installation is required and the account is granted sudo access, the Debian route would be `sudo apt-get update && sudo apt-get install ansible-core`.
- No system-wide `ansible.cfg`, remote inventory, SSH host configuration, credentials, or remote host configuration was created. The project-specific configuration is documented under `Ansible/`.
- No remote host was changed during verification; only `localhost` was tested with the local connection.
- `ansible-core` does not include the broad community collection bundle. Install only the collections needed for a particular project with `ansible-galaxy`.
- The virtual environment keeps Ansible separate from Debian's externally managed system Python and makes the installation removable without root access.
