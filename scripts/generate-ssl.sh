#!/bin/bash

# Script de génération de certificats SSL auto-signés pour développement
# Pour la production, utilisez Let's Encrypt ou des certificats commerciaux

SSL_DIR="./nginx/ssl"
CERT_FILE="$SSL_DIR/cert.pem"
KEY_FILE="$SSL_DIR/key.pem"

# Créer le répertoire SSL s'il n'existe pas
mkdir -p "$SSL_DIR"

# Générer un certificat auto-signé valide 365 jours
if [ ! -f "$CERT_FILE" ] || [ ! -f "$KEY_FILE" ]; then
    echo "Génération des certificats SSL auto-signés..."
    
    openssl req -x509 -nodes -days 365 -newkey rsa:2048 \
        -keyout "$KEY_FILE" \
        -out "$CERT_FILE" \
        -subj "/C=FR/ST=State/L=City/O=Development/OU=IT/CN=localhost"
    
    echo "Certificats générés avec succès dans $SSL_DIR"
    echo "ATTENTION: Ce sont des certificats auto-signés pour développement uniquement!"
else
    echo "Les certificats existent déjà dans $SSL_DIR"
fi

# Afficher les informations du certificat
openssl x509 -in "$CERT_FILE" -text -noout | grep -E "Subject:|Not Before:|Not After:"
