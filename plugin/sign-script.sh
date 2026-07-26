#!/bin/zsh
# openssl can only sign with a PEM/PKCS private key, NOT an OpenSSH-format one
PRIVATE_KEY_PATH=~/.ssh/id_rsa_pem
PUBLIC_KEY_PATH=~/.ssh/id_rsa.pub

PUBLIC_KEY_PKCS8=$(ssh-keygen -f "$PUBLIC_KEY_PATH" -e -m pkcs8 | tail -n +2 | ghead -n -1 | tr -d '\n')
echo "This is your public key: '$PUBLIC_KEY_PKCS8'"

if [ $# -eq 0 ]; then
  # No parameter provided, read from stdin
  DATA=$(cat)
else
  # Parameter provided, read from file
  DATA=$(cat "$1")
fi

SIGNATURE=$(echo -n "$DATA" | openssl dgst -sha512 -sign "$PRIVATE_KEY_PATH" | base64 | tr -d '\n')
echo "This is your signature: '$SIGNATURE'"
