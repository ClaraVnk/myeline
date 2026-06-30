#!/bin/bash
# Script de vérification du .gitignore
# Vérifie qu'aucun fichier sensible n'est tracké par Git

echo "🔍 Vérification du .gitignore - Sécurité et RGPD"
echo "================================================"

# Couleurs
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Compteurs
ERRORS=0
WARNINGS=0

# Fonction pour vérifier si des fichiers sont trackés
check_tracked() {
    local pattern=$1
    local description=$2
    local severity=$3  # "error" ou "warning"
    
    files=$(git ls-files | grep -E "$pattern" 2>/dev/null)
    
    if [ ! -z "$files" ]; then
        if [ "$severity" = "error" ]; then
            echo -e "${RED}❌ ERREUR${NC}: $description"
            ERRORS=$((ERRORS + 1))
        else
            echo -e "${YELLOW}⚠️  WARNING${NC}: $description"
            WARNINGS=$((WARNINGS + 1))
        fi
        echo "$files"
        echo ""
    fi
}

echo ""
echo "1️⃣  Vérification des fichiers de configuration sensibles..."
echo "-----------------------------------------------------------"

check_tracked "\.env$" "Fichier .env détecté (contient des secrets)" "error"
check_tracked "\.env\.local$" "Fichier .env.local détecté" "error"
check_tracked "\.env\.production$" "Fichier .env.production détecté" "error"
check_tracked "tokens\.env$" "Fichier tokens.env détecté" "error"
check_tracked "secrets\.json$" "Fichier secrets.json détecté" "error"

echo ""
echo "2️⃣  Vérification des certificats SSL..."
echo "-----------------------------------------------------------"

check_tracked "\.pem$" "Certificats .pem détectés" "error"
check_tracked "\.key$" "Clés privées .key détectées" "error"
check_tracked "\.crt$" "Certificats .crt détectés" "error"
check_tracked "\.csr$" "CSR détectés" "warning"

echo ""
echo "3️⃣  Vérification des bases de données..."
echo "-----------------------------------------------------------"

check_tracked "\.db$" "Fichiers .db détectés" "error"
check_tracked "\.sqlite$" "Fichiers .sqlite détectés" "error"
check_tracked "\.sqlite3$" "Fichiers .sqlite3 détectés" "error"

echo ""
echo "4️⃣  Vérification des données personnelles (RGPD)..."
echo "-----------------------------------------------------------"

check_tracked "uploads/.*\.(jpg|jpeg|png|pdf)$" "Fichiers uploadés (cartes étudiantes) détectés" "error"
check_tracked "student_cards/" "Dossier student_cards détecté" "error"
check_tracked "user_data/" "Dossier user_data détecté" "error"
check_tracked "personal_data/" "Dossier personal_data détecté" "error"
check_tracked "\.csv$" "Fichiers CSV (potentiellement données perso) détectés" "warning"
check_tracked "\.xlsx?$" "Fichiers Excel (potentiellement données perso) détectés" "warning"

echo ""
echo "5️⃣  Vérification des logs..."
echo "-----------------------------------------------------------"

check_tracked "logs/.*\.log$" "Fichiers de logs détectés" "warning"
check_tracked "\.log$" "Fichiers .log détectés" "warning"

echo ""
echo "6️⃣  Vérification des backups..."
echo "-----------------------------------------------------------"

check_tracked "\.bak$" "Fichiers .bak détectés" "warning"
check_tracked "\.backup$" "Fichiers .backup détectés" "warning"
check_tracked "backup/" "Dossier backup détecté" "warning"

echo ""
echo "7️⃣  Vérification des fichiers temporaires..."
echo "-----------------------------------------------------------"

check_tracked "__pycache__/" "Dossiers __pycache__ détectés" "warning"
check_tracked "\.pyc$" "Fichiers .pyc détectés" "warning"
check_tracked "\.tmp$" "Fichiers .tmp détectés" "warning"

echo ""
echo "8️⃣  Vérification des dossiers de données..."
echo "-----------------------------------------------------------"

check_tracked "data/cache/" "Dossier data/cache détecté" "warning"
check_tracked "data/raw/" "Dossier data/raw détecté" "warning"
check_tracked "data/processed/" "Dossier data/processed détecté" "warning"

echo ""
echo "9️⃣  Vérification des fichiers système..."
echo "-----------------------------------------------------------"

check_tracked "\.DS_Store$" "Fichiers .DS_Store (macOS) détectés" "warning"
check_tracked "Thumbs\.db$" "Fichiers Thumbs.db (Windows) détectés" "warning"
check_tracked "desktop\.ini$" "Fichiers desktop.ini (Windows) détectés" "warning"

echo ""
echo "🔟 Vérification des patterns de secrets..."
echo "-----------------------------------------------------------"

# Recherche de patterns suspects dans les fichiers trackés (exclure docs et README)
if git ls-files | grep -v "README.md" | grep -v "docs/" | xargs grep -l "SECRET_KEY\s*=\s*['\"]" 2>/dev/null | grep -v ".env.example" | grep -v ".gitignore"; then
    echo -e "${RED}❌ ERREUR${NC}: SECRET_KEY en dur détecté dans le code"
    ERRORS=$((ERRORS + 1))
fi

if git ls-files | grep -v "README.md" | grep -v "docs/" | xargs grep -l "API_KEY\s*=\s*['\"]" 2>/dev/null | grep -v ".env.example" | grep -v ".gitignore"; then
    echo -e "${RED}❌ ERREUR${NC}: API_KEY en dur détecté dans le code"
    ERRORS=$((ERRORS + 1))
fi

if git ls-files | grep -v "README.md" | grep -v "docs/" | xargs grep -l "password\s*=\s*['\"]" 2>/dev/null | grep -v ".env.example" | grep -v ".gitignore" | grep -v "test_"; then
    echo -e "${YELLOW}⚠️  WARNING${NC}: Mot de passe en dur détecté (vérifier si c'est un test)"
    WARNINGS=$((WARNINGS + 1))
fi

echo ""
echo "================================================"
echo "📊 Résumé de la vérification"
echo "================================================"

if [ $ERRORS -eq 0 ] && [ $WARNINGS -eq 0 ]; then
    echo -e "${GREEN}✅ Aucun problème détecté !${NC}"
    echo ""
    echo "Le .gitignore est correctement configuré."
    exit 0
else
    echo -e "${RED}Erreurs critiques : $ERRORS${NC}"
    echo -e "${YELLOW}Avertissements : $WARNINGS${NC}"
    echo ""
    
    if [ $ERRORS -gt 0 ]; then
        echo -e "${RED}⚠️  ACTION REQUISE :${NC}"
        echo "1. Supprimer les fichiers sensibles du repository"
        echo "2. Ajouter les patterns au .gitignore"
        echo "3. Utiliser git filter-branch ou BFG Repo-Cleaner pour nettoyer l'historique"
        echo ""
        echo "Commandes utiles :"
        echo "  git rm --cached <fichier>  # Retirer du tracking sans supprimer"
        echo "  git commit -m 'Remove sensitive files'"
        exit 1
    else
        echo -e "${YELLOW}ℹ️  Les avertissements peuvent être ignorés si intentionnels${NC}"
        exit 0
    fi
fi
