# Proposed architecture: direct Google model access

Local factory orchestration and VM workers, with direct calls to managed Gemini on Google Cloud. LiteLLM and the existing LiteLLM-backed Keybroker are removed from this proposed design. This is a target architecture, not a record of deployed services.

```mermaid
flowchart TB
    Users["Lab users"]
    Admins["Administrators"]
    IdP["Organization identity provider<br/>Workload identity federation source"]

    subgraph Server["ON-PREMISES SERVER — approximately 1 TB RAM"]
        direction TB

        subgraph Host["Proxmox VE host"]
            PVE["Hypervisor management<br/>VM lifecycle, storage, firewall"]
            Storage["Local SSD / NVMe storage<br/>VM disks and persistent volumes"]
        end

        subgraph Control["CONTROL VM — Debian + Docker Compose"]
            direction TB

            Tailnet["Tailscale<br/>Remote access"]
            Caddy["Caddy<br/>HTTPS application access"]
            WebUI["Open WebUI<br/>Mission interface"]
            Manager["Agent Manager<br/>Orchestration + direct Google model client"]

            Budget["Mission admission and accounting<br/>Team allowances, reservations, usage<br/>NEW IMPLEMENTATION REQUIRED"]
            DB[("PostgreSQL<br/>Missions, artifacts, identities,<br/>accounting and application state")]
            NATS["NATS JetStream<br/>Mission queue, status and resume events"]

            Forgejo["Forgejo<br/>Git repositories and human collaboration"]
            Monitor["Prometheus + Grafana<br/>Local health, capacity and mission metrics"]
            Ansible["Ansible<br/>Guest provisioning and configuration"]
        end

        subgraph Workers["WORKER VM POOL — private virtual network"]
            direction TB

            Worker["Worker supervisor — one per VM<br/>Unique identity, job lifecycle,<br/>credential handling and usage reporting"]

            subgraph Jobs["Docker job containers"]
                OMP["OMP runtime<br/>Planner → coder → reviewer<br/>Direct Google model client"]
                Workspace["Isolated mission workspace<br/>Artifacts and pause/resume checkpoints"]
            end
        end

        Egress["Controlled outbound network access<br/>Firewall + DNS + HTTPS<br/>Not an inference proxy"]
    end

    subgraph Google["GOOGLE CLOUD — lab project or separate team projects"]
        direction TB

        WIF["Workload Identity Federation<br/>Short-lived Google access tokens"]
        IAM["IAM + model-access policies<br/>Least-privilege workload identities"]
        Gemini["Managed Gemini serving endpoint<br/>Model selected through Model Garden"]
        CloudOps["Cloud Billing + spend-cap budgets<br/>Quotas, audit logs and usage visibility"]
    end

    Backup[("OFF-SERVER BACKUP TARGET<br/>VM backups + application-consistent data<br/>Restore-tested recovery copies")]

    Users --> Tailnet
    Admins --> Tailnet
    Tailnet --> Caddy
    Tailnet -->|"Restricted management access"| PVE

    Caddy --> WebUI
    Caddy --> Forgejo
    Caddy --> Monitor
    WebUI -->|"Submit mission / view results"| Manager

    Manager <-->|"Admission and reconciliation"| Budget
    Budget <--> DB
    Manager <--> DB
    Forgejo --> DB

    Manager -->|"Dispatch approved mission"| NATS
    NATS <-->|"Pull jobs / publish status"| Worker
    Worker -->|"Start, stop and supervise"| OMP
    OMP <--> Workspace
    Worker -->|"Artifacts and usage records"| Manager

    Manager -->|"Direct model requests"| Egress
    OMP -->|"Direct model requests"| Egress
    Egress -->|"Inference HTTPS"| Gemini
    Egress -->|"Authentication HTTPS"| WIF

    IdP -.->|"Federated workload identity"| WIF
    IAM -.->|"Authorizes access"| Gemini
    WIF -.->|"Short-lived credentials"| Manager
    WIF -.->|"Short-lived credentials"| Worker
    Worker -.->|"Scoped credential delivery"| OMP

    Gemini -.->|"Provider usage and audit records"| CloudOps
    CloudOps -.->|"Billing reconciliation"| Budget

    Ansible -->|"Create / configure VMs"| PVE
    Ansible -->|"Deploy worker configuration"| Worker

    PVE --- Storage
    PVE -.->|"Host metrics"| Monitor
    Manager -.->|"Application metrics"| Monitor
    Worker -.->|"Worker metrics"| Monitor

    PVE -->|"VM backups"| Backup
    DB -->|"Database backups"| Backup
    Forgejo -->|"Repositories and application data"| Backup
```

## Boundaries and assumptions

- Workers call Google directly; no LiteLLM or inference proxy remains.
- Local mission accounting, direct Google client integration, and VM provisioning automation require implementation.
- Google access tokens do not enforce per-mission dollar limits. Google project/service spend caps complement local accounting and are not instantaneous.
- Credential arrows describe logical delivery, not independent inbound connections from Google. Authentication and inference use controlled outbound HTTPS.
- The PostgreSQL node groups application databases conceptually; it does not prescribe merging the existing separate database services.
- Forgejo remains a collaboration service. The diagram does not imply that the current worker mission path checks out or pushes repositories.
- Worker VMs and application state remain on-premises, but model requests and their supplied content are processed in Google Cloud.
- View this file in a Markdown preview with Mermaid support, such as GitHub's rendered file view or a Mermaid-enabled editor preview.
