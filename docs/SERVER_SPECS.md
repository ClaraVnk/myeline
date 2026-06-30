# Server specs

Sizing guidance for the Myeline production deployment. **Revised
upward after a 4 vCPU / 8 GB box was observed pegging at 100% CPU on
a single concurrent RAG query** — the embedding pass alone consumes
2–4 cores, and the response stays slow until those finish. Plan for
*peak* usage, not idle.

> **Reality check.** A single RAG query orchestrates an Ollama
> embedding pass (`bge-m3`, 2–4 CPU cores at 100% for 0.5–2 s),
> a ChromaDB HNSW search (1–2 cores), 1–3 auxiliary LLM calls (HyDE,
> multi-query, reranker — Mistral cloud or local), and a synthesis
> call (Mistral cloud or local Ollama). With Mistral cloud the total
> local CPU peak is ~5 cores; with Ollama-only synthesis it can
> reach 10+ cores. Anything below 8 cores will visibly stutter
> under interactive use.

## TL;DR

| Profile | vCPU | RAM | Disk | Notes |
|---|---|---|---|---|
| **Single-user demo** (you running it for yourself) | **8** | **16 GB** | 50 GB SSD | Embedding only on CPU + Mistral cloud for synthesis. 4 cores will saturate on every query — go straight to 8 if you want a usable feel. |
| **SaaS – small (≤ 200 active users)** | 8 | 16 GB | 100 GB SSD | Same stack, gunicorn 9 workers default. Spikes are fine because users don't query simultaneously. |
| **SaaS – medium (≤ 2 000 active users)** | 16 | 32 GB | 300 GB SSD | Tune gunicorn to 16 workers. ChromaDB + audit log dominate disk growth. |
| **SaaS – large (≤ 10 000 active users)** | 32 | 64 GB | 1 TB NVMe | Add a MariaDB read-replica + Redis sentinel if you accept the operational cost. |
| **Sovereign / on-prem (small internal org, ≤ 20 users)** | **16** | **32 GB** | 200 GB NVMe | mistral-nemo Q4 (~7 GB) in Ollama RAM. Without a GPU expect 15–40 s per RAG query. |
| **Sovereign / on-prem (≤ 200 internal users)** | **24** | **64 GB** | 500 GB NVMe + GPU | RTX 4090 24 GB or L40S strongly recommended — without it, queries serialise on CPU and the second concurrent user waits. |
| **Sovereign / large org or 70B model** | 32 | 128 GB | 1 TB NVMe + 2× GPU | Llama 3.1 70B (Q4, ~40 GB VRAM) or Mixtral 8x7B. Multi-GPU for parallelism. |

## What actually consumes resources

### Memory (idle, SaaS profile)

| Component | RAM |
|---|---|
| `web` (gunicorn, 9 workers default) | ~1.5 GB |
| `worker` (RQ) | ~200 MB |
| `cron` sidecar (supercronic) | ~50 MB |
| `mariadb` (InnoDB buffer pool default) | ~1.0–1.5 GB |
| `redis` (cache + queue) | ~100 MB |
| `ollama` with bge-m3 loaded | ~700 MB |
| ChromaDB hot index (HNSW in process memory at query time) | 200–800 MB |
| **Total idle** | **~4 GB** |
| Headroom for peak / image pulls / backup tar streaming | +50% |
| **Recommended SaaS minimum** | **8 GB** |

### Memory (sovereign — local LLM)

Add the chosen Ollama model to the figures above:

| Local model | Quantisation | Resident RAM | Notes |
|---|---|---|---|
| `mistral-nemo` (12 B) | Q4_K_M | ~7 GB | Default fallback. Decent answers, runs on CPU but slow. |
| `mistral-nemo` | Q8 | ~13 GB | Better quality, needs more RAM. |
| `mixtral-8x7b` | Q4 | ~26 GB | Comparable to Mistral medium. CPU-only is slow (≥ 30 s per answer); GPU strongly recommended. |
| `llama3.1:70b` | Q4 | ~40 GB | Top-tier local quality. GPU mandatory in practice. |

**With a GPU**, models load into VRAM rather than system RAM. The
above figures still apply approximately to total system memory budget,
but the latency improves 5–20× depending on the GPU.

### CPU

| Workload | Demand |
|---|---|
| Web + worker steady-state | 1–2 cores |
| **Embedding (bge-m3 on CPU)** | **2–4 cores at 100% for 0.5–2 s per query** |
| ChromaDB HNSW search | 1–2 cores for ~100 ms |
| Auxiliary LLM calls (HyDE, multi-query, reranker) | network-bound on Mistral cloud; 4–8 cores each on local Ollama |
| **Local synthesis LLM (mistral-nemo on CPU)** | **4–8 cores at 100% for 5–30 s** |
| MariaDB | 1 core (2+ at peak) |
| Cron tasks (most run < 30 s) | bursts only |

**Single-user reality**: even an idle box answering one question
saturates 4 cores for the duration of the query because embedding +
HNSW + synthesis run in close succession. 8 cores is the practical
floor for an interactive feel; 4 cores is OK for batch / non-realtime
use only.

**Multi-user reality**: queries don't perfectly serialise. With
8 cores you can comfortably handle 1–2 simultaneous active queries;
beyond that they queue. Past 4 simultaneous users active *at the
same instant*, plan for 16+ cores.

Without a GPU, the sovereign profile **scales by core count, not RAM**
once the model fits. 8 cores answer ~1 query/min, 16 cores ~3/min,
24 cores ~6/min — for a single-user feel under sovereign mode you
basically need a GPU or you accept 15–40 s wait per question.

### Disk

| Use | Rough size | Notes |
|---|---|---|
| OS + container images | 5–10 GB | Slim Python image is ~600 MB; Ollama models are the big eaters. |
| Ollama models | bge-m3: 600 MB · mistral-nemo Q4: 7 GB · Q8: 13 GB | Stored in `data/ollama/`. |
| ChromaDB | ~10 KB per indexed chunk | 100 k chunks ≈ 1 GB; grows linearly with content + tenants. |
| MariaDB | 100 MB → 5 GB | Audit log + conversations dominate growth. |
| Uploads | unbounded | Sized by what users upload; cap via plan limits. |
| Backups (30-day retention) | 2–5× live data | `backup_databases` cron output. Stored under `data/backups/`. |
| Logs | ~10 MB/day | Rotated by Docker / journald. |
| **Comfortable starting point** | **50 GB SSD** | Doubles every 6–12 months at moderate growth. |

NVMe gives noticeably better p99 latency for both ChromaDB (HNSW seeks)
and MariaDB. SATA SSD is fine for SaaS small / medium.

### Network

- **Outbound** to Brevo, Mistral API, Stripe, GitHub container registry: ~10 Mbps sustained, more during image pulls.
- **Inbound** through Pangolin tunnel or direct HTTPS: scales with concurrent users; 100 Mbps suffices well past the medium tier.
- Latency to Mistral (Paris/Frankfurt) matters: aim for < 50 ms.

## Reference VPS / hardware

OVH VPS pricing as of 2026 — TTC, all-in. The "Recommended" mark is OVH's, and matches our floor for an interactive feel.

| Tier (Myeline) | OVH offer | Spec | TTC/mo | Match |
|---|---|---|---|---|
| Single-user / demo | **VPS-3** ⭐ | 8 vCores / 24 GB / 200 GB NVMe | **20,39 €** | Comfortable — embedding doesn't peg the box |
| SaaS small (≤ 200 users) | **VPS-3** ⭐ | 8 vCores / 24 GB / 200 GB NVMe | **20,39 €** | Same machine — quotas + plan limits keep concurrency low |
| SaaS medium (≤ 2 000 users) | VPS-4 | 12 vCores / 48 GB / 300 GB NVMe | 37,73 € | Extra headroom for peak hours; currently shown "Indispo" — fall back to VPS-3 + monitor |
| SaaS large (≤ 10 000 users) | VPS-5 / VPS-6 | 16–24 vCores / 64–96 GB / 350–400 GB NVMe | 56,09 €–74,45 € | Currently "Indispo" — consider Hetzner CCX or bare metal |
| Sovereign — small org (CPU only) | Hetzner AX42 (bare metal) | Ryzen 7 / 64 GB / 1 TB NVMe | ~50 € | CPU-only sovereign is *workable* (15–40 s per query) but not interactive |
| Sovereign — ≤ 200 users (with GPU) | Bare-metal Xeon + RTX 4090 24 GB | 64 GB / 1 TB NVMe + GPU | 250–400 € | GPU strongly recommended past 1 concurrent user |
| Sovereign — large org / 70B | Bare-metal + 2× L40S 48 GB | 64 GB / 1 TB NVMe + 2 GPU | 800–1500 € | Required for Llama 3.1 70B or Mixtral with reasonable latency |

> **Practical reading**: VPS-3 at 20,39 €/mo TTC is the floor for any
> production-feeling install (cloud SaaS or sovereign demo). The 4-vCore
> VPS-1 / VPS-2 will saturate on a single concurrent RAG query — fine for
> a personal sandbox, not for paying customers.

> **OVH availability flux**: the VPS-4 / VPS-5 / VPS-6 tiers regularly
> show up as "Indispo" on OVH's order page. When you grow past VPS-3,
> Hetzner CCX / dedicated bare-metal boxes are the practical fallback —
> often cheaper for the same vCPU count and with more predictable
> performance under load.

> **Why does a single query saturate a 4-core VPS?**
> Embedding + ChromaDB HNSW + synthesis prompt run in close
> succession. On a 4-core box, the embedding pass alone (which uses
> all 4 PyTorch threads by default) takes the CPU to 100% for the
> ~1 s it lasts. The browser waits until the response is rendered,
> so the user experience is "frozen tab for ~3 s, then answer". That
> machine works for *yourself*, but not for a customer demo.

## OS and runtime requirements

- **Linux distribution**: Rocky / AlmaLinux 9 / Debian 12 / Ubuntu 22.04 LTS or later.
- **Container runtime**: Podman 4.6+ (rootless mode used for the central instance + Enterprise tenant stacks). Docker also works but Myeline ships docker-compose syntax that podman-compose understands.
- **systemd**: required for unit-managed Podman pods if you go that route.
- **Python 3.11+** if running outside containers (the official path is containers).
- **SELinux**: works in Enforcing — the compose file already labels volumes with `:z` and the Enterprise provisioning code handles SELinux quirks (see `docker-compose.dev.yml` notes).
- **GPU drivers** (sovereign with GPU): NVIDIA drivers 535+ and Container Toolkit installed; Ollama auto-detects.

## What grows over time

These need monitoring; budget for upgrades when they cross the threshold:

- **MariaDB size**: `audit_logs` and `conversation_turns` are append-only.
  Audit archive cron purges past 180 d if `AUDIT_S3_BUCKET` is set;
  conversation pruning isn't automatic yet.
- **ChromaDB**: 10 KB per chunk, no automatic eviction. A user uploading
  a 500-page PDF generates ~500 chunks = 5 MB.
- **Backups directory**: 30-day rolling, but each daily snapshot can be
  several GB once the tenant base grows. Consider tiering older
  archives off-host (S3 / Glacier).
- **Log volume**: Gunicorn access logs stay on disk. Rotate via the
  container runtime or pipe to a centralised log store.

## Sizing the cron sidecar

The `cron` service runs `supercronic` and holds **almost no resources**
at idle (≤ 50 MB RAM, near-zero CPU). Each fired task forks a `flask
run-cron` subprocess that reuses the web image — these subprocesses
inherit the same memory profile as a single web worker (~200 MB) and
exit when done. The dominant cron CPU spike is `backup_databases`
(`mysqldump` + `tar`) which runs once daily; size the disk I/O
accordingly.
