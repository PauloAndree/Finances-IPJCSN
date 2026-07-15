@echo off
cd /d "%~dp0"

echo ============================================
echo  Iniciando o Sistema Financeiro IPJCSN...
echo ============================================
start "Sistema Financeiro - NAO FECHAR" cmd /k python app.py

echo Aguardando o sistema subir...
timeout /t 4 /nobreak >nul

echo ============================================
echo  Iniciando o link de acesso (Cloudflare)...
echo  O link novo vai aparecer abaixo, procure por "trycloudflare.com"
echo  NAO FECHE ESTA JANELA enquanto quiser manter o link ativo.
echo ============================================
"C:\Program Files (x86)\cloudflared\cloudflared.exe" tunnel --url http://127.0.0.1:5000

pause
