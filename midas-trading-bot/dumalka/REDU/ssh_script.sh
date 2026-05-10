#!/bin/bash
sshpass -p '123QWEasd!.!' ssh -o StrictHostKeyChecking=no -tt ab@100.117.168.63 << 'SSH_EOF'
echo '123QWEasd!.!' | sudo -S docker ps --format "{{.Names}}"
SSH_EOF
