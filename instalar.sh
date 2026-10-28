#!/data/data/com.termux/files/usr/bin/bash
# Instalador do Robô Cruzeiro no celular (Termux)
# Uso no Termux:
#   curl -fsSL https://raw.githubusercontent.com/SEU_USUARIO/robo-cruzeiro/main/instalar.sh -o instalar.sh && bash instalar.sh SEU_USUARIO

set -e
USUARIO_GITHUB="$1"
if [ -z "$USUARIO_GITHUB" ]; then
  echo "Faltou o seu usuário do GitHub. Ex.: bash instalar.sh brunomantos"
  exit 1
fi

echo ""
echo "=== Robô Cruzeiro — instalação no celular ==="
echo ""
echo "1/5 Instalando programas (pode demorar alguns minutos)..."
yes | pkg update -y >/dev/null 2>&1 || true
pkg install -y python cronie termux-services curl >/dev/null
pip install --quiet requests beautifulsoup4

echo "2/5 Seus dados do CallMeBot (ficam só neste celular)"
read -r -p "   Número do WhatsApp (ex.: +5531999999999): " FONE
read -r -p "   Apikey do CallMeBot: " CHAVE

echo "3/5 Criando a pasta do robô..."
mkdir -p "$HOME/robo"
cat > "$HOME/robo/rodar.sh" <<EOF
#!/data/data/com.termux/files/usr/bin/bash
# Roda o robô uma vez. O Agendador (cron) chama este arquivo a cada 10 min.
cd "\$HOME/robo"
export CALLMEBOT_PHONE='$FONE'
export CALLMEBOT_APIKEY='$CHAVE'
export ROBO_SITES='OLX,Mercado Livre'
BASE="https://raw.githubusercontent.com/$USUARIO_GITHUB/robo-cruzeiro/main"
# baixa a versão mais nova do script e dos termos (se falhar, usa a anterior)
for ARQ in monitor.py termos.txt; do
  curl -fsSL "\$BASE/\$ARQ" -o "\$ARQ.novo" && mv "\$ARQ.novo" "\$ARQ" || rm -f "\$ARQ.novo"
done
python monitor.py >> log.txt 2>&1
# guarda só as últimas 3000 linhas do log
tail -n 3000 log.txt > log.tmp && mv log.tmp log.txt
EOF
chmod +x "$HOME/robo/rodar.sh"

echo "4/5 Agendando para rodar a cada 10 minutos..."
( crontab -l 2>/dev/null | grep -v 'robo/rodar.sh' ; echo "*/10 * * * * \$HOME/robo/rodar.sh" ) | crontab -
mkdir -p "$HOME/.termux/boot"
cat > "$HOME/.termux/boot/iniciar-robo.sh" <<'EOF'
#!/data/data/com.termux/files/usr/bin/bash
# Religa o robô sozinho quando o celular reinicia (precisa do app Termux:Boot)
termux-wake-lock
crond
EOF
chmod +x "$HOME/.termux/boot/iniciar-robo.sh"
termux-wake-lock || true
pgrep crond >/dev/null || crond

echo "5/5 Primeira rodada (só memoriza os anúncios atuais)..."
bash "$HOME/robo/rodar.sh" || true
echo ""
tail -n 40 "$HOME/robo/log.txt" || true
echo ""
echo "=== Pronto! O robô vai rodar a cada 10 minutos. ==="
echo "Para ver o que ele fez:   tail -n 40 ~/robo/log.txt"
