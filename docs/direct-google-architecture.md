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

            Worker["Worker supervisor — one per VM<br/>Unique workload identity,<br/>job lifecycle and usage reporting"]

            subgraph Jobs["Docker job containers"]
                OMP["OMP runtime<br/>Planner → coder → reviewer<br/>Model requests via mission gateway"]
                Workspace["Isolated mission workspace<br/>Artifacts and pause/resume checkpoints"]
            end

            VertexGateway["Per-mission Vertex gateway<br/>WIF / ADC token handling<br/>Model allowlist and usage metering"]

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
    Worker -->|"Create / scope gateway"| VertexGateway
    OMP <--> Workspace
    OMP -->|"Model requests; no Google credential"| VertexGateway
    Worker -->|"Artifacts and usage records"| Manager

    Manager -->|"Direct model requests"| Egress
    VertexGateway -->|"Authenticated Vertex inference"| Egress
    Egress -->|"Inference HTTPS"| Gemini
    Egress -->|"WIF token exchange HTTPS"| WIF

    IdP -.->|"Federated workload identity"| WIF
    IAM -.->|"Authorizes access"| Gemini
    WIF -.->|"Short-lived credentials"| Manager
    WIF -.->|"Short-lived credentials"| VertexGateway

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

## Google identity, tokens, and billing

For production Vertex AI calls, use Application Default Credentials (ADC)
backed by Workload Identity Federation (WIF) and Google's auth libraries.
Google's Gemini Enterprise Agent Platform guide recommends API keys for
testing and ADC for production
([authentication guidance](https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/start/gcp-auth)).
WIF supports on-premises workloads without service-account private keys; the
auth libraries exchange an external subject token through Google STS and obtain
short-lived Google credentials
([WIF](https://docs.cloud.google.com/iam/docs/workload-identity-federation),
[auth libraries](https://docs.cloud.google.com/iam/docs/authenticate-with-auth-libraries)).
Use direct federated IAM access where the selected Vertex endpoint supports
it; otherwise impersonate a dedicated, least-privilege service account.
Restrict pool/provider mappings and conditions to the intended Manager or
worker identities.

Keep the external-account credential configuration and subject-token source
under the trusted worker supervisor. The supervisor creates a per-mission
Vertex gateway; only that gateway obtains and refreshes Google access tokens.
OMP can run model-driven tools and code, so a token or WIF configuration in its
environment or filesystem is readable by the job. OMP instead sends model
requests over a private per-run path to the gateway, which enforces the approved
project, location, model, and request policy before forwarding them. Do not
make this a general-purpose proxy. Destroy the gateway after the mission. This
gateway and its usage controls are target requirements, not deployed features.

The deployed LiteLLM-backed Keybroker issues LiteLLM virtual keys only; it
does not mint Google credentials. Confirm the exact Model Garden endpoint,
supported auth mode, project boundaries, pricing, usage reporting, and required
egress before implementation. If the supplied Vertex service requires an API
key, keep it in a dedicated secret manager behind the trusted gateway; never
pass the master key through mission metadata, NATS, or an OMP job.

OAuth token expiry is not a spending limit. Enforce Google Cloud quotas and
billing budgets alongside local mission accounting, and reconcile observed
provider usage.

## Boundaries and assumptions

- The trusted Agent Manager calls Vertex AI directly; worker OMP calls pass through the per-mission credential gateway. LiteLLM is removed from this target.
- Local mission accounting, the Vertex gateway, direct Google client integration, and VM provisioning automation require implementation.
- Google access tokens do not enforce per-mission dollar limits. Google project/service spend caps complement local accounting and are not instantaneous.
- Credential arrows describe logical delivery, not independent inbound connections from Google. Authentication and inference use controlled outbound HTTPS.
- The PostgreSQL node groups application databases conceptually; it does not prescribe merging the existing separate database services.
- Forgejo remains a collaboration service. The diagram does not imply that the current worker mission path checks out or pushes repositories.
- Worker VMs and application state remain on-premises, but model requests and their supplied content are processed in Google Cloud.
- View this file in a Markdown preview with Mermaid support, such as GitHub's rendered file view or a Mermaid-enabled editor preview.
