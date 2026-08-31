# Новый сервер: безопасный порядок настройки

Делайте это из **консоли VPS-провайдера** или при открытом втором SSH-сеансе.
Не закрывайте текущий сеанс, пока новый пользователь не войдёт по ключу во
втором окне.

## 1. Сначала — доступ по ключу

На своём компьютере создайте отдельный ключ с парольной фразой:

```bash
ssh-keygen -t ed25519 -a 100 -f ~/.ssh/autoposting_admin
```

На сервере через консоль провайдера создайте обычного администратора и добавьте
**только публичную** часть этого ключа в `/home/deploy/.ssh/authorized_keys`:

```bash
sudo adduser deploy
sudo usermod -aG sudo deploy
sudo install -d -m 700 -o deploy -g deploy /home/deploy/.ssh
sudoedit /home/deploy/.ssh/authorized_keys
sudo chown deploy:deploy /home/deploy/.ssh/authorized_keys
sudo chmod 600 /home/deploy/.ssh/authorized_keys
```

Откройте второе окно и проверьте вход:

```bash
ssh -i ~/.ssh/autoposting_admin deploy@SERVER_IP
```

Только после успешной проверки создайте файл
`/etc/ssh/sshd_config.d/90-autoposting.conf` через `sudoedit`:

```text
PermitRootLogin no
PasswordAuthentication no
KbdInteractiveAuthentication no
PubkeyAuthentication yes
AllowUsers deploy
```

Проверьте конфигурацию и примените её:

```bash
sudo sshd -t
sudo systemctl reload ssh
```

Не меняйте SSH-порт «для защиты»: это не заменяет ключи и усложняет поддержку.

## 2. Закройте входящие порты

В firewall у VPS-провайдера разрешите только:

- TCP 80 и 443 — всем, для сайта;
- TCP 22 — только с вашего постоянного публичного IP или через VPN/Tailscale.

Повторите правило на Ubuntu через UFW:

```bash
sudo apt update
sudo apt install -y ufw
sudo ufw default deny incoming
sudo ufw default allow outgoing
sudo ufw allow 80/tcp
sudo ufw allow 443/tcp
sudo ufw allow from YOUR_PUBLIC_IP to any port 22 proto tcp
sudo ufw enable
sudo ufw status verbose
```

Если у вас меняется IP, временно используйте `sudo ufw limit 22/tcp`, а затем
настройте Tailscale/WireGuard и запретите публичный SSH. Не открывайте PostgreSQL
(`5432`), Redis (`6379`) или Docker API наружу. В `compose.yaml` этого проекта
PostgreSQL и веб-приложение вообще не публикуют порты на хосте; наружу смотрит
лишь Nginx на 80/443.

## 3. Fail2ban и обновления

После ключевого SSH добавьте Fail2ban как дополнительный слой:

```bash
sudo apt install -y fail2ban unattended-upgrades
sudoedit /etc/fail2ban/jail.d/sshd.local
```

Содержимое файла:

```ini
[sshd]
enabled = true
maxretry = 5
findtime = 10m
bantime = 1h
```

Затем:

```bash
sudo systemctl enable --now fail2ban
sudo fail2ban-client status sshd
sudo systemctl enable --now unattended-upgrades
```

## 4. Перед запуском приложения

- Войдите в панели VPS, GitHub, доменного регистратора и Zernio с MFA.
- Не переносите старые Docker volumes или старый `/root`.
- Скопируйте новый проект в приватный репозиторий, создайте `.env` с правами
  `600`, а затем запустите `docker compose up -d --build`.
- Не добавляйте `deploy` в группу `docker`: она фактически даёт права root.
  Запускайте Docker через ограниченный `sudo` либо от отдельного
  администратора, которому доверяете как root.
- Сразу настройте резервную копию PostgreSQL и проверьте восстановление на
  отдельной машине.
