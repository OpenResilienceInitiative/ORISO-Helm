#!/bin/bash
# Switches the AddonService on for Pre-Dev with generated credentials (kept in the lane's
# extras, mode 600). Idempotent: existing values are kept. Run helm-check/helm-upgrade after.
source "$(dirname "$0")/lib.sh"
has_extra global.secrets.addonServiceDbPassword || set_extra global.secrets.addonServiceDbPassword "$(openssl rand -hex 24)"
if ! has_extra addonService.tokenPrivateKeyPem.file; then
	install -m 600 /dev/null "$EXTRA/addonService.tokenPrivateKeyPem.file"
	openssl genpkey -algorithm RSA -pkeyopt rsa_keygen_bits:3072 -out "$EXTRA/addonService.tokenPrivateKeyPem.file" 2>/dev/null
fi
set_extra addonService.enabled.bool true
set_extra addonService.discovery.createNamespace.bool true
set_extra addonService.image.tag "${ADDONSERVICE_TAG:-dev}"
echo "addonService switched on in the lane extras (image tag ${ADDONSERVICE_TAG:-dev})"
