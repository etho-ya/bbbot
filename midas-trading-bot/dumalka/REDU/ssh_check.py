import paramiko

ssh = paramiko.SSHClient()
ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
try:
    ssh.connect('100.117.168.63', username='ab', password='123QWEasd!.!')
    # Run a command with sudo
    stdin, stdout, stderr = ssh.exec_command('sudo -S find /opt -name "docker-compose.yml" 2>/dev/null')
    stdin.write('123QWEasd!.!\n')
    stdin.flush()
    print("Files found:", stdout.read().decode())
    
    stdin, stdout, stderr = ssh.exec_command('sudo -S docker ps --format "{{.Names}}"')
    stdin.write('123QWEasd!.!\n')
    stdin.flush()
    containers = stdout.read().decode().strip().split('\n')
    print("Containers:", containers)
    
    for container in containers:
        if 'bot' in container or 'app' in container:
            print(f"\n--- Logs for {container} ---\n")
            stdin, stdout, stderr = ssh.exec_command(f'sudo -S docker logs --tail=50 {container}')
            stdin.write('123QWEasd!.!\n')
            stdin.flush()
            print(stdout.read().decode())
            
    print("\n--- .env files ---\n")
    stdin, stdout, stderr = ssh.exec_command('sudo -S find /opt -name ".env" | xargs grep -H DUMALKA')
    stdin.write('123QWEasd!.!\n')
    stdin.flush()
    print(stdout.read().decode())
finally:
    ssh.close()
