#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────
#   Myeline — guided installer
#   Usage:  ./scripts/install.sh
# ─────────────────────────────────────────────────────────────────
#
# Walks the operator through a fresh deployment in one of three
# flavours and produces a fully filled .env, then brings the stack
# up via Podman:
#
#   1. classic    — SaaS multi-tenant on a VPS (myeline.io style)
#   2. sovereign  — air-gapped on-prem (license required, no external API)
#   3. hybrid     — on-prem with BYOK external APIs (license required)
#
# Each category of variables (mailer, AI, OAuth providers, cloud
# connectors, backups…) is offered as a yes/no block. If the user
# skips a block we leave a clearly marked placeholder with a TODO
# comment in .env so they can come back to it.
#
# Idempotent: if .env already exists, the script offers to back it
# up before clobbering.

set -euo pipefail

# ─── ANSI colours ────────────────────────────────────────────────
if [ -t 1 ]; then
  C_BOLD=$(tput bold 2>/dev/null || echo "")
  C_DIM=$(tput dim 2>/dev/null || echo "")
  C_RED=$(tput setaf 1 2>/dev/null || echo "")
  C_GREEN=$(tput setaf 2 2>/dev/null || echo "")
  C_YELLOW=$(tput setaf 3 2>/dev/null || echo "")
  C_BLUE=$(tput setaf 4 2>/dev/null || echo "")
  C_MAGENTA=$(tput setaf 5 2>/dev/null || echo "")
  C_CYAN=$(tput setaf 6 2>/dev/null || echo "")
  C_RESET=$(tput sgr0 2>/dev/null || echo "")
else
  C_BOLD="" C_DIM="" C_RED="" C_GREEN="" C_YELLOW="" \
  C_BLUE="" C_MAGENTA="" C_CYAN="" C_RESET=""
fi

info()    { printf "%s[i]%s %s\n" "$C_BLUE" "$C_RESET" "$*"; }
ok()      { printf "%s[✓]%s %s\n" "$C_GREEN" "$C_RESET" "$*"; }
warn()    { printf "%s[!]%s %s\n" "$C_YELLOW" "$C_RESET" "$*" >&2; }
fatal()   { printf "%s[✗]%s %s\n" "$C_RED" "$C_RESET" "$*" >&2; exit 1; }
section() {
  printf "\n%s%s── %s%s\n" "$C_BOLD" "$C_CYAN" "$*" "$C_RESET"
}
hint()    { printf "    %s%s%s\n" "$C_DIM" "$*" "$C_RESET"; }

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

# ─── Language selection ─────────────────────────────────────────────
# Default to FR (the project's native language). User picks at start;
# the choice gates every visible string via the t() helper below.
# NOTE: detailed help text in deeper sections is still FR-only — i18n
# coverage is incremental, see issue #i18n-install-script.
LANG_CHOICE="fr"

ask_language() {
  printf "\n"
  printf "  ${C_BOLD}Language / Langue${C_RESET}\n\n"
  printf "    1) Français   ${C_DIM}(défaut / default)${C_RESET}\n"
  printf "    2) English\n\n"
  read -r -p "    [1/2] : " _l
  case "$_l" in
    2|en|EN|english|English) LANG_CHOICE="en" ;;
    *) LANG_CHOICE="fr" ;;
  esac
}

# t <key> — emit a translated string. Keys cover the user-visible
# anchor text. Anything not declared here falls through silently
# (the caller already had French inline).
t() {
  local k="$1"
  if [ "$LANG_CHOICE" = "en" ]; then
    case "$k" in
      welcome_title)   printf "Hello 👋\nWelcome to the Myeline installer. This script will\nguide you through deploying the platform on your server.\n" ;;
      welcome_time)    printf "Plan for ${C_BOLD}10 to 20 minutes${C_RESET}: 5 min of questions, 5–15 min\nof image pulls depending on your connection." ;;
      welcome_options) printf "For each section you can:\n  • enter values directly if you have them at hand\n  • ${C_BOLD}skip${C_RESET} and fill them later in .env (annotated placeholder)\n  • abort at any time with ${C_BOLD}Ctrl-C${C_RESET}" ;;
      press_enter)     printf "Press Enter to start… " ;;
      preflight)       printf "System pre-checks" ;;
      mode_section)    printf "Choose deployment mode" ;;
      mode_choice)     printf "Your choice" ;;
      mode_classic)    printf "${C_BOLD}1) Classic install${C_RESET} — multi-tenant SaaS (myeline.io style)\n   Stripe for billing, Mistral cloud for synthesis, all\n   cloud connectors, central mode for Enterprise stacks.\n   ${C_DIM}No license required.${C_RESET}" ;;
      mode_sov)        printf "${C_BOLD}2) Sovereign install${C_RESET} — air-gap, 100%% on-prem\n   Local synthesis via Ollama, no external API call,\n   mailer in log-only mode, no public cloud connectors.\n   Designed for regulated sectors (health, defence, banking).\n   ${C_YELLOW}License required.${C_RESET}" ;;
      mode_hybrid)     printf "${C_BOLD}3) Sovereign-hybrid install${C_RESET} — on-prem + external APIs\n   Run Myeline on your infra but with your own API keys\n   (Mistral, Claude, OpenAI, Gemini). Cloud connectors\n   active. No Stripe.\n   ${C_YELLOW}License required.${C_RESET}" ;;
      done_title)      printf "Installation complete 🎉" ;;
      next_steps)      printf "Next steps" ;;
      docs)            printf "Documentation" ;;
      support)         printf "Support" ;;
      missing_choice)  printf "Invalid choice. Type 1, 2 or 3." ;;
      backup_existing) printf "Existing configuration detected" ;;
      backup_offer)    printf "Backup and overwrite?" ;;
      backup_done)     printf "Backup created" ;;
      install_aborted) printf "Installation aborted." ;;
      stack_starting)  printf "Starting the stack" ;;
      stack_pulling)   printf "Pulling / building images (may take a few minutes)…" ;;
      stack_starting_services) printf "Starting services…" ;;
      mariadb_waiting) printf "Waiting for MariaDB to be ready…" ;;
      mariadb_ready)   printf "MariaDB ready" ;;
      mariadb_timeout) printf "MariaDB did not respond after 60 s. Check logs:" ;;
      provisioning)    printf "DB migrations + admin account creation…" ;;
      stack_ready)     printf "Stack operational" ;;
      start_now)       printf "Start the stack now?" ;;
      start_later)     printf "Stack not started. To launch later:" ;;
      *) return 1 ;;
    esac
  else
    case "$k" in
      welcome_title)   printf "Bonjour 👋\nBienvenue sur l'installeur Myeline. Ce script va vous guider pas\nà pas dans le déploiement de la plateforme sur votre serveur.\n" ;;
      welcome_time)    printf "Comptez ${C_BOLD}10 à 20 minutes${C_RESET} : 5 min de questions, 5–15 min\nde pull d'images selon votre connexion." ;;
      welcome_options) printf "Pour chaque section, vous pourrez :\n  • saisir directement les valeurs si vous les avez sous la main\n  • ${C_BOLD}skipper${C_RESET} et compléter plus tard dans .env (placeholder annoté)\n  • interrompre à tout moment avec ${C_BOLD}Ctrl-C${C_RESET}" ;;
      press_enter)     printf "Appuyez sur Entrée pour démarrer… " ;;
      preflight)       printf "Pré-vérifications du système" ;;
      mode_section)    printf "Choix du mode de déploiement" ;;
      mode_choice)     printf "Votre choix" ;;
      mode_classic)    printf "${C_BOLD}1) Installation classique${C_RESET} — SaaS multi-tenant (style myeline.io)\n   Stripe pour les abonnements, Mistral cloud pour la synthèse,\n   tous les connecteurs cloud, mode central pour Enterprise.\n   ${C_DIM}Pas de licence requise.${C_RESET}" ;;
      mode_sov)        printf "${C_BOLD}2) Installation souveraine${C_RESET} — air-gap, 100%% on-prem\n   Synthèse locale via Ollama, aucun appel API externe, mailer\n   en log-only, aucun connecteur cloud public. Idéal secteurs\n   régulés (santé, défense, banque).\n   ${C_YELLOW}Clé de licence requise.${C_RESET}" ;;
      mode_hybrid)     printf "${C_BOLD}3) Installation souverain-hybride${C_RESET} — on-prem + APIs externes\n   Vous tournez Myeline sur votre infra mais avec vos propres\n   clés API (Mistral, Claude, OpenAI, Gemini). Connecteurs\n   cloud activés. Pas de Stripe.\n   ${C_YELLOW}Clé de licence requise.${C_RESET}" ;;
      done_title)      printf "Installation terminée 🎉" ;;
      next_steps)      printf "Étapes suivantes" ;;
      docs)            printf "Documentation" ;;
      support)         printf "Support" ;;
      missing_choice)  printf "Choix invalide. Tapez 1, 2 ou 3." ;;
      backup_existing) printf "Configuration existante détectée" ;;
      backup_offer)    printf "Sauvegarder et écraser ?" ;;
      backup_done)     printf "Sauvegarde créée" ;;
      install_aborted) printf "Installation annulée." ;;
      stack_starting)  printf "Démarrage de la stack" ;;
      stack_pulling)   printf "Pull / build des images (peut prendre quelques minutes)…" ;;
      stack_starting_services) printf "Démarrage des services…" ;;
      mariadb_waiting) printf "Attente que MariaDB soit prêt…" ;;
      mariadb_ready)   printf "MariaDB prêt" ;;
      mariadb_timeout) printf "MariaDB ne répond pas après 60 s. Vérifier les logs :" ;;
      provisioning)    printf "Migrations DB + création du compte administrateur…" ;;
      stack_ready)     printf "Stack opérationnelle" ;;
      start_now)       printf "Démarrer la stack maintenant ?" ;;
      start_later)     printf "Stack non démarrée. Pour la lancer plus tard :" ;;
      *) return 1 ;;
    esac
  fi
}

# ─── Generic prompt helpers ──────────────────────────────────────
# prompt_var <name> <human label> [default] [validation_regex]
# Stores the answer in a global variable named after $1 (uppercased).
prompt_var() {
  local var_name="$1"
  local label="$2"
  local default="${3:-}"
  local regex="${4:-}"
  local answer prompt

  while true; do
    if [ -n "$default" ]; then
      prompt="    ${label} [${default}] : "
    else
      prompt="    ${label} : "
    fi
    read -r -p "$prompt" answer
    answer="${answer:-$default}"
    if [ -n "$regex" ] && [ -n "$answer" ] && ! [[ "$answer" =~ $regex ]]; then
      warn "Format invalide."
      continue
    fi
    eval "$var_name=\"\$answer\""
    return
  done
}

# prompt_secret <name> <label> — same but hidden input.
prompt_secret() {
  local var_name="$1"
  local label="$2"
  local answer
  read -r -s -p "    ${label} : " answer; echo
  eval "$var_name=\"\$answer\""
}

# yes_no <question> [default Y|N]  →  returns 0 if yes, 1 if no.
yes_no() {
  local q="$1"; local default="${2:-N}"; local prompt ans
  if [ "$default" = "Y" ]; then prompt="[O/n]"; else prompt="[o/N]"; fi
  read -r -p "    ${q} ${prompt} " ans
  if [ -z "$ans" ]; then ans="$default"; fi
  case "$ans" in
    [oOyY]*) return 0 ;;
    *)       return 1 ;;
  esac
}

# ─── Banner ──────────────────────────────────────────────────────
banner() {
  printf "\n${C_BOLD}${C_MAGENTA}╔═══════════════════════════════════════════════════════════╗\n"
  printf "║                                                           ║\n"
  printf "║    ${C_CYAN}m y e l i n e${C_MAGENTA}    —  guided installer / installateur    ║\n"
  printf "║                                                           ║\n"
  printf "║    ${C_RESET}${C_DIM}self-hosted RAG platform, FR / EU-hosted${C_MAGENTA}              ║\n"
  printf "║    ${C_RESET}${C_DIM}https://myeline.io${C_MAGENTA}                                    ║\n"
  printf "║                                                           ║\n"
  printf "╚═══════════════════════════════════════════════════════════╝${C_RESET}\n\n"
  t welcome_title; printf "\n\n"
  t welcome_time;  printf "\n\n"
  t welcome_options; printf "\n\n"
  read -r -p "    $(t press_enter)" _
}

# ─── Pre-flight checks ───────────────────────────────────────────
preflight() {
  section "$(t preflight)"

  if [ "$(id -u)" -eq 0 ]; then
    warn "Le script tourne en root. Préférable en utilisateur ordinaire."
  fi

  if command -v podman >/dev/null 2>&1; then
    CONTAINER_ENGINE="podman"
    if ! command -v podman-compose >/dev/null 2>&1; then
      fatal "podman-compose introuvable. Installez-le : pip install podman-compose."
    fi
    COMPOSE_CMD="podman-compose"
    ok "Podman $(podman --version | awk '{print $3}') détecté"
  elif command -v docker >/dev/null 2>&1; then
    CONTAINER_ENGINE="docker"
    if docker compose version >/dev/null 2>&1; then
      COMPOSE_CMD="docker compose"
    elif command -v docker-compose >/dev/null 2>&1; then
      COMPOSE_CMD="docker-compose"
    else
      fatal "docker compose introuvable. Installez Docker Compose v2."
    fi
    ok "Docker $(docker --version | awk '{print $3}' | tr -d ',') détecté (Podman recommandé)"
  else
    fatal "Aucun moteur de containers trouvé. Installez Podman : https://podman.io/getting-started/installation"
  fi

  local free_gb
  free_gb=$(df -BG "$ROOT_DIR" | awk 'NR==2 {print $4}' | tr -d 'G')
  if [ "$free_gb" -lt 20 ]; then
    warn "Seulement ${free_gb} GB libres — 50+ GB recommandés."
  else
    ok "${free_gb} GB d'espace disque disponibles"
  fi

  local total_ram_gb
  total_ram_gb=$(awk '/MemTotal/ {printf "%.0f", $2/1024/1024}' /proc/meminfo)
  if [ "$total_ram_gb" -lt 6 ]; then
    warn "Seulement ${total_ram_gb} GB de RAM — 8+ GB recommandés (voir docs/SERVER_SPECS.md)."
  else
    ok "${total_ram_gb} GB de RAM"
  fi

  local cores
  cores=$(nproc)
  if [ "$cores" -lt 4 ]; then
    warn "Seulement ${cores} cœurs CPU — 8+ recommandés pour usage interactif."
  else
    ok "${cores} cœurs CPU"
  fi
}

# ─── Existing .env handling ──────────────────────────────────────
backup_existing_env() {
  if [ -f .env ]; then
    section "$(t backup_existing)"
    info "$([ "$LANG_CHOICE" = "en" ] \
      && echo "An .env file already exists at the project root." \
      || echo "Un fichier .env existe déjà à la racine du projet.")"
    if yes_no "$(t backup_offer)" "Y"; then
      local backup=".env.backup-$(date +%Y%m%d-%H%M%S)"
      cp .env "$backup"
      ok "$(t backup_done) : $backup"
    else
      fatal "$(t install_aborted)"
    fi
  fi
}

# ─── Mode selection ──────────────────────────────────────────────
choose_mode() {
  section "$(t mode_section)"
  printf "\n  "; t mode_classic; printf "\n\n  "; t mode_sov; printf "\n\n  "; t mode_hybrid; printf "\n\n"
  while true; do
    read -r -p "    $(t mode_choice) [1/2/3] : " mode_choice
    case "$mode_choice" in
      1) DEPLOYMENT_MODE="classic"; break ;;
      2) DEPLOYMENT_MODE="sovereign"; break ;;
      3) DEPLOYMENT_MODE="hybrid"; break ;;
      *) warn "$(t missing_choice)" ;;
    esac
  done
  ok "Mode : ${C_BOLD}${DEPLOYMENT_MODE}${C_RESET}"
}

# ─── License key (required in all modes) ─────────────────────────
ask_license_key() {
  section "Clé de licence Myeline"
  cat <<EOF

  Une clé de licence Myeline est nécessaire pour tous les modes
  d'installation. Format attendu :

      ${C_DIM}MYE-<base64url-payload>.<base64url-signature>${C_RESET}

  La licence est ${C_BOLD}signée Ed25519${C_RESET} et vérifiée hors-ligne au
  démarrage de l'app — aucun appel réseau requis (compatible
  air-gap pour le mode souverain).

  Sans clé pour l'instant : tapez ${C_DIM}skip${C_RESET}. Le .env sera généré
  avec un placeholder ; l'app refusera de démarrer en production
  jusqu'à ce qu'une clé valide soit fournie.

  Pas de licence du tout ? → ${C_CYAN}hello@myeline.io${C_RESET}

EOF
  while true; do
    read -r -p "    Clé de licence : " LICENSE_KEY
    if [ "$LICENSE_KEY" = "skip" ]; then
      LICENSE_KEY=""
      warn "Licence non fournie — pensez à la renseigner avant le démarrage."
      break
    fi
    # Accept the new signed format MYE-<b64url-payload>.<b64url-sig>.
    # We only do a structural format check here; cryptographic verification
    # happens at the next app boot (see app/services/license.py).
    if [[ "$LICENSE_KEY" =~ ^MYE-[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+$ ]]; then
      ok "Format de licence valide (vérification cryptographique au boot)"
      break
    fi
    if [ -z "$LICENSE_KEY" ]; then
      warn "Tapez 'skip' pour continuer sans licence pour l'instant."
    else
      warn "Format invalide. Attendu : MYE-<payload>.<signature> (base64url)."
    fi
  done
}

# ─── Domain + URL ────────────────────────────────────────────────
ask_domain() {
  section "Domaine public"
  cat <<EOF

  Le domaine sur lequel l'instance sera accessible. Utilisé pour :
   • les liens absolus dans les emails
   • les redirect_uri OAuth (chaque provider doit avoir
     ce domaine dans sa liste blanche)
   • la page de status publique /status

EOF
  prompt_var PUBLIC_DOMAIN "Domaine (sans https://)" "myeline.local" \
    "^([a-zA-Z0-9]([a-zA-Z0-9-]*[a-zA-Z0-9])?\.)+[a-zA-Z]{2,}$|^[a-zA-Z][a-zA-Z0-9-]*$"
  EXTERNAL_URL="https://${PUBLIC_DOMAIN}"
  OAUTH_REDIRECT_BASE_URL="$EXTERNAL_URL"
  ok "URL publique : ${C_CYAN}${EXTERNAL_URL}${C_RESET}"
}

# ─── Admin account ───────────────────────────────────────────────
ask_admin() {
  section "Compte administrateur initial"
  cat <<EOF

  Ce compte aura tous les droits sur l'instance (admin dashboard,
  cron, audit log, provisionnement Enterprise).

EOF
  prompt_var ADMIN_EMAIL "Email admin" "" \
    "^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$"
  while true; do
    prompt_secret ADMIN_PASSWORD "Mot de passe (≥ 12 caractères)"
    if [ "${#ADMIN_PASSWORD}" -lt 12 ]; then
      warn "Trop court (${#ADMIN_PASSWORD} caractères, 12 minimum)."
      continue
    fi
    local confirm
    read -r -s -p "    Confirmer : " confirm; echo
    if [ "$ADMIN_PASSWORD" = "$confirm" ]; then
      ok "Compte admin configuré"
      break
    fi
    warn "Les mots de passe ne correspondent pas."
  done
}

# ─── Mailer (Brevo) ─────────────────────────────────────────────
ask_mailer() {
  section "Emails transactionnels (Brevo / Sendinblue)"
  hint "Inscriptions, confirmations, alertes, digests…"

  if [ "$DEPLOYMENT_MODE" = "sovereign" ]; then
    cat <<EOF

  En mode souverain, le mailer est forcé en ${C_BOLD}log-only${C_RESET}
  (les emails sont écrits dans logs/mailer/ au lieu d'être envoyés).
  Vous pouvez quand même fournir une clé Brevo si vous voulez la
  réactiver ponctuellement.

EOF
  fi

  if yes_no "Configurer Brevo maintenant ?" "$([ "$DEPLOYMENT_MODE" = "sovereign" ] && echo N || echo Y)"; then
    hint "Récupérer la clé : app.brevo.com → SMTP & API → Clés API"
    prompt_var BREVO_API_KEY "Clé API Brevo (xkeysib-…)" "" \
      "^xkeysib-[a-f0-9]{64}-[A-Za-z0-9]+$"
    prompt_var MAIL_DEFAULT_SENDER "Expéditeur par défaut" "hello@${PUBLIC_DOMAIN}" \
      "^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$"
    prompt_var MAIL_SENDER_NAME "Nom d'expéditeur" "Myeline"
    ok "Mailer configuré"
  else
    BREVO_API_KEY="dummy"
    MAIL_DEFAULT_SENDER="hello@${PUBLIC_DOMAIN}"
    MAIL_SENDER_NAME="Myeline"
    info "Mailer en mode dry-run (emails écrits dans les logs)."
  fi
}

# ─── reCAPTCHA (classic only, optional everywhere) ───────────────
ask_recaptcha() {
  if [ "$DEPLOYMENT_MODE" = "sovereign" ]; then
    RECAPTCHA_SITE_KEY=""; RECAPTCHA_SECRET_KEY=""
    DISABLE_RECAPTCHA=true
    return
  fi
  section "Google reCAPTCHA v3 (anti-bot inscription)"
  hint "Optionnel. Sans reCAPTCHA, des bots peuvent créer des comptes."
  if yes_no "Activer reCAPTCHA ?" "N"; then
    hint "Récupérer les clés : google.com/recaptcha/admin"
    prompt_var RECAPTCHA_SITE_KEY "Site key (publique)"
    prompt_var RECAPTCHA_SECRET_KEY "Secret key"
    DISABLE_RECAPTCHA=false
  else
    RECAPTCHA_SITE_KEY=""; RECAPTCHA_SECRET_KEY=""
    DISABLE_RECAPTCHA=true
  fi
}

# ─── AI / LLM providers ──────────────────────────────────────────
ask_ai() {
  section "Synthèse IA"
  hint "L'embedding (bge-m3) est ${C_BOLD}toujours local via Ollama${C_RESET}, quel que soit le mode."

  case "$DEPLOYMENT_MODE" in
    sovereign)
      cat <<EOF

  En souverain, toute la synthèse passe par Ollama local.

EOF
      prompt_var OLLAMA_BASE_URL "URL Ollama" "http://ollama:11434"
      prompt_var OLLAMA_EMBED_MODEL "Modèle d'embedding" "bge-m3"
      prompt_var OLLAMA_LLM_MODEL "Modèle LLM de synthèse" "mistral-nemo"
      MISTRAL_API_KEY=""
      MISTRAL_LLM_MODEL=""
      MISTRAL_AUX_MODEL=""
      info "Pensez à pull le modèle après l'install : ollama pull $OLLAMA_LLM_MODEL"
      ;;
    classic|hybrid)
      OLLAMA_BASE_URL="http://ollama:11434"
      OLLAMA_EMBED_MODEL="bge-m3"
      cat <<EOF

  Mistral est le provider par défaut (français, hébergé UE, RGPD-compatible).
  En hybride, chaque organisation Enterprise pourra basculer vers OpenAI /
  Anthropic / Gemini avec sa propre clé via /admin/orgs (BYOK).

EOF
      if yes_no "Configurer la clé Mistral plateforme ?" "Y"; then
        hint "Récupérer la clé : console.mistral.ai → API Keys"
        prompt_var MISTRAL_API_KEY "Clé Mistral AI"
        prompt_var MISTRAL_LLM_MODEL "Modèle de synthèse" "mistral-medium-latest"
        prompt_var MISTRAL_AUX_MODEL "Modèle auxiliaire (HyDE/rerank)" "mistral-small-latest"
      else
        MISTRAL_API_KEY=""
        MISTRAL_LLM_MODEL="mistral-medium-latest"
        MISTRAL_AUX_MODEL="mistral-small-latest"
        prompt_var OLLAMA_LLM_MODEL "Modèle LLM Ollama de fallback" "mistral-nemo"
      fi
      ;;
  esac
}

# ─── Stripe (classic only) ───────────────────────────────────────
ask_stripe() {
  STRIPE_SECRET_KEY=""; STRIPE_PUBLISHABLE_KEY=""; STRIPE_WEBHOOK_SECRET=""
  STRIPE_PRICE_ID=""; STRIPE_PRICE_ID_PRO=""
  STRIPE_PRICE_ID_TEAM=""; STRIPE_PRICE_ID_ENTERPRISE=""

  if [ "$DEPLOYMENT_MODE" != "classic" ]; then return; fi

  section "Paiements Stripe"
  hint "Nécessaire uniquement pour les abonnements payants. En souverain/hybride, le paiement est out-of-band (licence)."
  if yes_no "Activer Stripe ?" "N"; then
    hint "Dashboard Stripe : https://dashboard.stripe.com/apikeys"
    prompt_var STRIPE_SECRET_KEY "Secret Key (sk_live_…)" "" "^sk_(live|test)_[A-Za-z0-9]+$"
    prompt_var STRIPE_PUBLISHABLE_KEY "Publishable Key (pk_live_…)" "" "^pk_(live|test)_[A-Za-z0-9]+$"
    hint "Webhook secret : Dashboard → Developers → Webhooks → Signing secret"
    prompt_var STRIPE_WEBHOOK_SECRET "Webhook Signing Secret (whsec_…)" "" "^whsec_[A-Za-z0-9]+$"

    cat <<EOF

  Price IDs des 4 plans (laisser vide pour désactiver le plan).
  Récupérables dans Stripe → Products → chaque produit.

EOF
    prompt_var STRIPE_PRICE_ID "Price ID — Standard (9.90 €/mo)"
    prompt_var STRIPE_PRICE_ID_PRO "Price ID — Pro (29.90 €/mo)"
    prompt_var STRIPE_PRICE_ID_TEAM "Price ID — Team (99 €/mo)"
    prompt_var STRIPE_PRICE_ID_ENTERPRISE "Price ID — Enterprise (299 €/mo)"
  fi
}

# ─── OAuth login (Google / Microsoft / Apple) ────────────────────
ask_oauth_login() {
  GOOGLE_LOGIN_CLIENT_ID=""; GOOGLE_LOGIN_CLIENT_SECRET=""
  MICROSOFT_LOGIN_CLIENT_ID=""; MICROSOFT_LOGIN_CLIENT_SECRET=""
  APPLE_LOGIN_CLIENT_ID=""; APPLE_LOGIN_PRIVATE_KEY=""
  APPLE_LOGIN_KEY_ID=""; APPLE_LOGIN_TEAM_ID=""

  # On-prem deployments (sovereign + hybrid) deliberately disable social
  # login. Sovereign can't reach Google/Microsoft anyway (air-gap), and in
  # hybrid the model is "company employees with their own IdP" — tying
  # accounts to personal Gmail/Hotmail is anti-pattern. Use OIDC enterprise
  # SSO via /org/<slug>/oidc instead.
  if [ "$DEPLOYMENT_MODE" != "classic" ]; then
    info "Connexion sociale désactivée en mode ${DEPLOYMENT_MODE} — utilisez OIDC enterprise via /org/<slug>/oidc."
    return
  fi

  section "Connexion sociale (login OAuth)"
  hint "Permet aux utilisateurs de se connecter avec Google/Microsoft/Apple."

  if yes_no "Activer Google Login ?" "N"; then
    hint "console.cloud.google.com → APIs & Services → Credentials → OAuth 2.0 client ID (Web application)"
    hint "Redirect URI à autoriser : ${EXTERNAL_URL}/auth/social/google/callback"
    prompt_var GOOGLE_LOGIN_CLIENT_ID "client_id"
    prompt_var GOOGLE_LOGIN_CLIENT_SECRET "client_secret"
  fi

  if yes_no "Activer Microsoft Login ?" "N"; then
    hint "portal.azure.com → App registrations → New registration"
    hint "Redirect URI : ${EXTERNAL_URL}/auth/social/microsoft/callback"
    prompt_var MICROSOFT_LOGIN_CLIENT_ID "client_id"
    prompt_var MICROSOFT_LOGIN_CLIENT_SECRET "client_secret"
  fi

  if yes_no "Activer Apple Login ?" "N"; then
    hint "developer.apple.com → Sign in with Apple"
    hint "Redirect URI : ${EXTERNAL_URL}/auth/social/apple/callback"
    prompt_var APPLE_LOGIN_CLIENT_ID "client_id (Service ID)"
    prompt_var APPLE_LOGIN_TEAM_ID "Team ID"
    prompt_var APPLE_LOGIN_KEY_ID "Key ID"
    prompt_var APPLE_LOGIN_PRIVATE_KEY "Private key (chemin du .p8 ou contenu)"
  fi
}

# ─── Cloud connectors (storage) ──────────────────────────────────
ask_cloud_connectors() {
  GOOGLE_CLIENT_ID=""; GOOGLE_CLIENT_SECRET=""
  ONEDRIVE_CLIENT_ID=""; ONEDRIVE_CLIENT_SECRET=""
  DROPBOX_APP_KEY=""; DROPBOX_APP_SECRET=""
  KDRIVE_CLIENT_ID=""; KDRIVE_CLIENT_SECRET=""

  if [ "$DEPLOYMENT_MODE" = "sovereign" ]; then
    info "Mode souverain : seuls S3 / WebDAV / kDrive (API token) seront proposés à l'utilisateur. Aucune clé OAuth à configurer."
    return
  fi

  section "Connecteurs de stockage cloud"
  hint "Permet aux utilisateurs d'indexer leurs Google Drive / OneDrive / Dropbox / kDrive."

  # In hybrid (souverain-hybride), the operator runs Myeline on their
  # own infra under a different domain — they MUST use their own OAuth
  # apps because the redirect URI is bound to that domain. Make this
  # explicit so they don't try to reuse Myeline's central credentials.
  if [ "$DEPLOYMENT_MODE" = "hybrid" ]; then
    cat <<EOF

  ${C_BOLD}${C_YELLOW}IMPORTANT — BYOC (Bring Your Own Credentials)${C_RESET}

  Vous tournez sur ${C_CYAN}${EXTERNAL_URL}${C_RESET}, donc les redirect URIs
  pointent vers ce domaine. ${C_BOLD}Vous devez créer vos propres apps OAuth${C_RESET}
  chez chaque provider (Google / Microsoft / Dropbox) et autoriser
  vos URIs ; les credentials de myeline.io ne fonctionneront pas
  ici.

  Walkthrough complet : ${C_DIM}docs/deployment-sovereign-hybrid.md${C_RESET}
  Reconfigurable plus tard depuis ${C_DIM}/admin/cloud-providers${C_RESET}.

EOF
  fi

  if yes_no "Activer Google Drive ?" "N"; then
    hint "Mêmes credentials que Google Login OK. Redirect URI :"
    hint "${EXTERNAL_URL}/user/cloud/gdrive/callback"
    prompt_var GOOGLE_CLIENT_ID "client_id"
    prompt_var GOOGLE_CLIENT_SECRET "client_secret"
  fi

  if yes_no "Activer OneDrive ?" "N"; then
    hint "Redirect URI : ${EXTERNAL_URL}/user/cloud/onedrive/callback"
    prompt_var ONEDRIVE_CLIENT_ID "client_id"
    prompt_var ONEDRIVE_CLIENT_SECRET "client_secret"
  fi

  if yes_no "Activer Dropbox ?" "N"; then
    hint "Redirect URI : ${EXTERNAL_URL}/user/cloud/dropbox/callback"
    prompt_var DROPBOX_APP_KEY "app_key"
    prompt_var DROPBOX_APP_SECRET "app_secret"
  fi

  if yes_no "Activer kDrive Infomaniak (OAuth) ?" "N"; then
    hint "developer.infomaniak.com — redirect URI :"
    hint "${EXTERNAL_URL}/user/cloud/kdrive/callback"
    prompt_var KDRIVE_CLIENT_ID "client_id"
    prompt_var KDRIVE_CLIENT_SECRET "client_secret"
  fi

  cat <<EOF

  ${C_DIM}Note : Zotero / S3 / Notion / WebDAV / kDrive (par token API) ne nécessitent
  pas de configuration globale ici — chaque utilisateur saisit ses
  identifiants directement depuis /user/cloud.${C_RESET}

EOF
}

# ─── Backups off-host ────────────────────────────────────────────
ask_backups() {
  BACKUP_REMOTE=""; RCLONE_CONFIG_PATH=""

  section "Backups hors-site (rclone)"
  hint "Sauvegarde quotidienne (DB + ChromaDB + uploads) vers Google Drive, S3, OneDrive, MinIO, etc."
  hint "Sans ça, les backups restent sur disque local — vulnérables à un crash matériel ou ransomware."

  if yes_no "Configurer un backup hors-site maintenant ?" "N"; then
    hint "Pré-requis : avoir lancé 'rclone config' sur l'hôte au préalable."
    hint "Exemples : gdrive:myeline-backups | s3-paris:myeline-prod-backups"
    prompt_var BACKUP_REMOTE "Remote rclone (nom:chemin)"
    prompt_var RCLONE_CONFIG_PATH "Chemin du rclone.conf" "$HOME/.config/rclone/rclone.conf"
    info "Voir docs/BACKUP.md pour la procédure complète."
  fi
}

# ─── Pangolin tunnel (optional) ──────────────────────────────────
ask_pangolin() {
  PANGOLIN_URL=""; NEWT_ID=""; NEWT_SECRET=""

  section "Tunnel Pangolin / Newt (optionnel)"
  hint "Tunnel reverse-proxy Fossorial — utile si votre VPS est derrière NAT ou pour mutualiser le HTTPS."

  if yes_no "Configurer un tunnel Pangolin ?" "N"; then
    prompt_var PANGOLIN_URL "URL serveur Pangolin"
    prompt_var NEWT_ID "Newt ID"
    prompt_var NEWT_SECRET "Newt secret"
  fi
}

# ─── Enterprise provisioning (classic only) ──────────────────────
ask_enterprise() {
  PODMAN_SOCKET_PATH=""; ENTERPRISE_IMAGE=""; SUPPORT_NOTIFY_EMAIL=""

  if [ "$DEPLOYMENT_MODE" != "classic" ]; then return; fi

  section "Provisionnement Enterprise (instance centrale uniquement)"
  hint "Si cette instance vend des stacks dédiées Enterprise, elle pilote Podman pour les provisionner."

  if yes_no "Activer le provisionnement Enterprise ?" "N"; then
    local default_socket="/run/user/$(id -u)/podman/podman.sock"
    prompt_var PODMAN_SOCKET_PATH "Socket Podman du host" "$default_socket"
    prompt_var ENTERPRISE_IMAGE "Image OCI des stacks Enterprise" "rg.fr-par.scw.cloud/myeline/web:latest"
    prompt_var SUPPORT_NOTIFY_EMAIL "Email notification nouveaux tickets" "$ADMIN_EMAIL"
    info "Activer le socket Podman avant le démarrage : systemctl --user enable --now podman.socket"
  fi
}

# ─── Secret generation ───────────────────────────────────────────
gen_secret() {
  if command -v python3 >/dev/null 2>&1; then
    python3 -c "import secrets; print(secrets.token_hex($1))"
  elif command -v openssl >/dev/null 2>&1; then
    openssl rand -hex "$1"
  else
    head -c "$(($1 * 2))" /dev/urandom | base64 | tr -dc 'a-f0-9' | head -c "$(($1 * 2))"
  fi
}

gen_urlsafe_secret() {
  if command -v python3 >/dev/null 2>&1; then
    python3 -c "import secrets; print(secrets.token_urlsafe($1))"
  else
    head -c "$1" /dev/urandom | base64 | tr -d '\n=/+' | head -c "$1"
  fi
}

# ─── .env writer ─────────────────────────────────────────────────
write_env() {
  section "Génération du fichier .env"

  local secret_key security_salt cloud_token_key db_password mysql_root_pwd
  secret_key=$(gen_secret 32)
  security_salt=$(gen_secret 32)
  cloud_token_key=$(gen_urlsafe_secret 48)
  db_password=$(gen_urlsafe_secret 24)
  mysql_root_pwd=$(gen_urlsafe_secret 24)

  local sovereign_flag=false
  [ "$DEPLOYMENT_MODE" = "sovereign" ] && sovereign_flag=true

  local mail_dry_run=false
  if [ "$DEPLOYMENT_MODE" = "sovereign" ] || [ "${BREVO_API_KEY:-dummy}" = "dummy" ]; then
    mail_dry_run=true
  fi

  cat > .env <<EOF
# ─── Generated by scripts/install.sh on $(date -u +%Y-%m-%dT%H:%M:%SZ) ───
# Mode: $DEPLOYMENT_MODE
# Domain: $EXTERNAL_URL
# Admin: $ADMIN_EMAIL
#
# Tout placeholder marqué TODO doit être remplacé avant le démarrage si la
# fonctionnalité associée doit être active. Ils sont sans danger laissés
# tels quels (la fonctionnalité reste désactivée).

# ──────────────────────────────────────────────────────────────────
# Deployment mode
# ──────────────────────────────────────────────────────────────────
DEPLOYMENT_MODE=$DEPLOYMENT_MODE
SOVEREIGN_MODE=$sovereign_flag
LICENSE_KEY=${LICENSE_KEY:-# TODO: collez ici votre clé MYE-XXXX-XXXX-XXXX-XXXX}

# ──────────────────────────────────────────────────────────────────
# Public URLs
# ──────────────────────────────────────────────────────────────────
PUBLIC_DOMAIN=$PUBLIC_DOMAIN
EXTERNAL_URL=$EXTERNAL_URL
OAUTH_REDIRECT_BASE_URL=$OAUTH_REDIRECT_BASE_URL

# ──────────────────────────────────────────────────────────────────
# Flask + security (auto-generated, do NOT regenerate without rotating)
# ──────────────────────────────────────────────────────────────────
FLASK_ENV=production
FLASK_DEBUG=false
TESTING=false
SECRET_KEY=$secret_key
SECURITY_PASSWORD_SALT=$security_salt
CLOUD_TOKEN_KEY=$cloud_token_key
SESSION_ENCRYPTION_KEY=
FILE_ENCRYPTION_KEY=
WTF_CSRF_ENABLED=true
WTF_CSRF_TIME_LIMIT=3600
SESSION_COOKIE_SECURE=true
SESSION_COOKIE_HTTPONLY=true
SESSION_COOKIE_SAMESITE=Lax
PERMANENT_SESSION_LIFETIME=3600

# ──────────────────────────────────────────────────────────────────
# Database
# ──────────────────────────────────────────────────────────────────
DATABASE_URL=mysql+pymysql://myeline_user:$db_password@mariadb:3306/myeline
SQLALCHEMY_DATABASE_URI=mysql+pymysql://myeline_user:$db_password@mariadb:3306/myeline
SQLALCHEMY_ECHO=false
DB_HOST=mariadb
DB_PORT=3306
DB_NAME=myeline
DB_USER=myeline_user
DB_PASSWORD=$db_password
MYSQL_ROOT_PASSWORD=$mysql_root_pwd
MYSQL_PASSWORD=$db_password

# ──────────────────────────────────────────────────────────────────
# Compose / runtime
# ──────────────────────────────────────────────────────────────────
COMPOSE_PROJECT_NAME=myeline
CONTAINER_ENGINE=$CONTAINER_ENGINE

# ──────────────────────────────────────────────────────────────────
# Uploads / logging / rate limiting
# ──────────────────────────────────────────────────────────────────
MAX_CONTENT_LENGTH=104857600
UPLOAD_FOLDER=uploads
LOG_LEVEL=INFO
LOG_TO_STDOUT=false
RATELIMIT_DEFAULT=200/hour
RATELIMIT_STORAGE_URL=redis://redis:6379/1
DISABLE_RATE_LIMITING=false

# ──────────────────────────────────────────────────────────────────
# Redis
# ──────────────────────────────────────────────────────────────────
REDIS_URL=redis://redis:6379/0
FLASK_LIMITER_STORAGE_URI=redis://redis:6379

# ──────────────────────────────────────────────────────────────────
# Mailer (Brevo)
# ──────────────────────────────────────────────────────────────────
BREVO_API_KEY=${BREVO_API_KEY:-dummy}
MAIL_DEFAULT_SENDER=${MAIL_DEFAULT_SENDER:-hello@$PUBLIC_DOMAIN}
MAIL_SENDER_NAME=${MAIL_SENDER_NAME:-Myeline}
MAIL_DRY_RUN=$mail_dry_run
MAIL_FORCE_SEND=false

# ──────────────────────────────────────────────────────────────────
# reCAPTCHA
# ──────────────────────────────────────────────────────────────────
RECAPTCHA_SITE_KEY=${RECAPTCHA_SITE_KEY:-}
RECAPTCHA_SECRET_KEY=${RECAPTCHA_SECRET_KEY:-}
DISABLE_RECAPTCHA=${DISABLE_RECAPTCHA:-true}

# ──────────────────────────────────────────────────────────────────
# AI / RAG
# ──────────────────────────────────────────────────────────────────
OLLAMA_BASE_URL=${OLLAMA_BASE_URL:-http://ollama:11434}
OLLAMA_EMBED_MODEL=${OLLAMA_EMBED_MODEL:-bge-m3}
OLLAMA_LLM_MODEL=${OLLAMA_LLM_MODEL:-}

MISTRAL_API_KEY=${MISTRAL_API_KEY:-}
MISTRAL_LLM_MODEL=${MISTRAL_LLM_MODEL:-mistral-medium-latest}
MISTRAL_AUX_MODEL=${MISTRAL_AUX_MODEL:-mistral-small-latest}

# ──────────────────────────────────────────────────────────────────
# Stripe (classic mode only — left empty otherwise)
# ──────────────────────────────────────────────────────────────────
STRIPE_SECRET_KEY=${STRIPE_SECRET_KEY:-}
STRIPE_PUBLISHABLE_KEY=${STRIPE_PUBLISHABLE_KEY:-}
STRIPE_WEBHOOK_SECRET=${STRIPE_WEBHOOK_SECRET:-}
STRIPE_PRICE_ID=${STRIPE_PRICE_ID:-}
STRIPE_PRICE_ID_PRO=${STRIPE_PRICE_ID_PRO:-}
STRIPE_PRICE_ID_TEAM=${STRIPE_PRICE_ID_TEAM:-}
STRIPE_PRICE_ID_ENTERPRISE=${STRIPE_PRICE_ID_ENTERPRISE:-}

# ──────────────────────────────────────────────────────────────────
# OAuth — connexion sociale
# ──────────────────────────────────────────────────────────────────
GOOGLE_LOGIN_CLIENT_ID=${GOOGLE_LOGIN_CLIENT_ID:-}
GOOGLE_LOGIN_CLIENT_SECRET=${GOOGLE_LOGIN_CLIENT_SECRET:-}
MICROSOFT_LOGIN_CLIENT_ID=${MICROSOFT_LOGIN_CLIENT_ID:-}
MICROSOFT_LOGIN_CLIENT_SECRET=${MICROSOFT_LOGIN_CLIENT_SECRET:-}
APPLE_LOGIN_CLIENT_ID=${APPLE_LOGIN_CLIENT_ID:-}
APPLE_LOGIN_PRIVATE_KEY=${APPLE_LOGIN_PRIVATE_KEY:-}
APPLE_LOGIN_KEY_ID=${APPLE_LOGIN_KEY_ID:-}
APPLE_LOGIN_TEAM_ID=${APPLE_LOGIN_TEAM_ID:-}

# ──────────────────────────────────────────────────────────────────
# Cloud storage connectors
# ──────────────────────────────────────────────────────────────────
GOOGLE_CLIENT_ID=${GOOGLE_CLIENT_ID:-}
GOOGLE_CLIENT_SECRET=${GOOGLE_CLIENT_SECRET:-}
ONEDRIVE_CLIENT_ID=${ONEDRIVE_CLIENT_ID:-}
ONEDRIVE_CLIENT_SECRET=${ONEDRIVE_CLIENT_SECRET:-}
DROPBOX_APP_KEY=${DROPBOX_APP_KEY:-}
DROPBOX_APP_SECRET=${DROPBOX_APP_SECRET:-}
KDRIVE_CLIENT_ID=${KDRIVE_CLIENT_ID:-}
KDRIVE_CLIENT_SECRET=${KDRIVE_CLIENT_SECRET:-}

# ──────────────────────────────────────────────────────────────────
# Pangolin / Newt tunnel
# ──────────────────────────────────────────────────────────────────
PANGOLIN_URL=${PANGOLIN_URL:-}
NEWT_ID=${NEWT_ID:-}
NEWT_SECRET=${NEWT_SECRET:-}

# ──────────────────────────────────────────────────────────────────
# Enterprise provisioning (classic only)
# ──────────────────────────────────────────────────────────────────
PODMAN_SOCKET_PATH=${PODMAN_SOCKET_PATH:-}
ENTERPRISE_IMAGE=${ENTERPRISE_IMAGE:-rg.fr-par.scw.cloud/myeline/web:latest}
SUPPORT_NOTIFY_EMAIL=${SUPPORT_NOTIFY_EMAIL:-$ADMIN_EMAIL}

# ──────────────────────────────────────────────────────────────────
# Backups off-host (rclone)
# ──────────────────────────────────────────────────────────────────
BACKUP_REMOTE=${BACKUP_REMOTE:-}
RCLONE_CONFIG_PATH=${RCLONE_CONFIG_PATH:-}
EOF

  chmod 600 .env
  ok ".env généré ($(wc -l < .env) lignes, mode 600)"
}

# ─── Stack bring-up ──────────────────────────────────────────────
bring_up_stack() {
  section "$(t stack_starting)"

  # Map deployment mode → compose file. classic uses the canonical
  # docker-compose.yml; sovereign and hybrid have dedicated files.
  local compose_file
  case "$DEPLOYMENT_MODE" in
    classic)   compose_file="docker-compose.yml" ;;
    sovereign) compose_file="docker-compose.sovereign.yml" ;;
    hybrid)    compose_file="docker-compose.hybrid.yml" ;;
    *)         fatal "Unknown DEPLOYMENT_MODE / DEPLOYMENT_MODE inconnu : $DEPLOYMENT_MODE" ;;
  esac

  if [ ! -f "$compose_file" ]; then
    fatal "Compose file not found / introuvable : $compose_file (mode=$DEPLOYMENT_MODE)"
  fi
  ok "Compose : $compose_file"

  info "$(t stack_pulling)"
  $COMPOSE_CMD -f "$compose_file" pull --ignore-pull-failures || true
  $COMPOSE_CMD -f "$compose_file" build

  info "$(t stack_starting_services)"
  $COMPOSE_CMD -f "$compose_file" up -d

  info "$(t mariadb_waiting)"
  local retries=30
  while ! $COMPOSE_CMD -f "$compose_file" exec -T mariadb mariadb -uroot -p"${MYSQL_ROOT_PASSWORD:-root_password}" -e 'SELECT 1' >/dev/null 2>&1; do
    retries=$((retries - 1))
    if [ "$retries" -le 0 ]; then
      fatal "$(t mariadb_timeout) $COMPOSE_CMD logs mariadb"
    fi
    sleep 2
  done
  ok "$(t mariadb_ready)"

  info "$(t provisioning)"
  # provision-init runs `flask db upgrade` then creates/promotes the admin
  # in a single transaction.
  $COMPOSE_CMD -f "$compose_file" exec -T web flask provision-init "$ADMIN_EMAIL" "$ADMIN_PASSWORD"

  ok "$(t stack_ready)"
}

# ─── Final summary ───────────────────────────────────────────────
final_summary() {
  cat <<EOF

${C_BOLD}${C_GREEN}═══════════════════════════════════════════════════════════
   Installation terminée 🎉
═══════════════════════════════════════════════════════════${C_RESET}

  Mode               : ${C_BOLD}$DEPLOYMENT_MODE${C_RESET}
  Domaine            : ${C_CYAN}$EXTERNAL_URL${C_RESET}
  Email admin        : $ADMIN_EMAIL
  Configuration      : ${C_DIM}$ROOT_DIR/.env${C_RESET}

  Logs en direct     : $COMPOSE_CMD logs -f web
  Healthcheck        : curl http://localhost:5000/healthz
  Page de status     : ${C_CYAN}$EXTERNAL_URL/status${C_RESET}

EOF

  # Highlight any TODO placeholders remaining in .env so the operator
  # knows what's still to fill in.
  if grep -q "^# TODO\|^[A-Z_]*=# TODO" .env 2>/dev/null; then
    cat <<EOF
  ${C_YELLOW}Variables encore à compléter dans .env${C_RESET} :
EOF
    grep -nE "^# TODO\|^[A-Z_]*=# TODO\|=$" .env | head -20 | while read -r line; do
      printf "    %s%s%s\n" "$C_DIM" "$line" "$C_RESET"
    done
    echo
  fi

  case "$DEPLOYMENT_MODE" in
    classic)
      cat <<EOF
  ${C_YELLOW}Étapes suivantes${C_RESET} :
   1. Configurer le reverse-proxy (Caddy / Nginx / Traefik) pour
      pointer https://$PUBLIC_DOMAIN vers localhost:5000.
   2. Compléter les clés Stripe / OAuth si nécessaire dans .env.
   3. Vérifier les fréquences de cron : scripts/crontab.
EOF
      ;;
    sovereign)
      cat <<EOF
  ${C_YELLOW}Étapes suivantes${C_RESET} :
   1. Vérifier qu'Ollama tourne et que ${C_BOLD}${OLLAMA_LLM_MODEL:-mistral-nemo}${C_RESET}
      est pull : ollama pull ${OLLAMA_LLM_MODEL:-mistral-nemo}
   2. Configurer le reverse-proxy interne pour HTTPS.
   3. Aucun trafic vers Internet — vérifiez vos règles firewall.
   4. Voir docs/deployment-sovereign.md pour le hardening.
EOF
      ;;
    hybrid)
      cat <<EOF
  ${C_YELLOW}Étapes suivantes${C_RESET} :
   1. Configurer le reverse-proxy pour HTTPS.
   2. Pour chaque organisation, basculer vers le provider IA voulu
      (Mistral / Anthropic / OpenAI / Gemini) via /admin/orgs.
   3. Compléter les clés OAuth manquantes dans .env si besoin.
EOF
      ;;
  esac

  cat <<EOF

  ${C_BOLD}Documentation${C_RESET} :
    docs/SERVER_SPECS.md             — sizing serveur
    docs/BACKUP.md                   — backups + rclone
    docs/EMAIL.md                    — config Brevo et kill-switches
    docs/deployment-sovereign.md     — hardening souverain

  ${C_BOLD}Support${C_RESET} : hello@myeline.io

EOF
}

# ─── Main ────────────────────────────────────────────────────────
main() {
  ask_language
  banner
  preflight
  backup_existing_env

  # 1. Mode + license
  choose_mode
  ask_license_key

  # 2. Public URL + admin
  ask_domain
  ask_admin

  # 3. Mailer
  ask_mailer

  # 4. reCAPTCHA
  ask_recaptcha

  # 5. AI providers
  ask_ai

  # 6. Stripe (classic only)
  ask_stripe

  # 7. OAuth login
  ask_oauth_login

  # 8. Cloud connectors
  ask_cloud_connectors

  # 9. Backups off-host
  ask_backups

  # 10. Pangolin tunnel
  ask_pangolin

  # 11. Enterprise provisioning (classic only)
  ask_enterprise

  # 12. Write .env + bring up
  write_env

  echo
  if yes_no "$(t start_now)" "Y"; then
    bring_up_stack
  else
    info "$(t start_later) $COMPOSE_CMD up -d"
  fi

  final_summary
}

main "$@"
