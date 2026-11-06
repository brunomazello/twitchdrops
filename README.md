<div align="center">

<img src="icons/pickaxe.ico" width="72" alt="">

# Twitch Drops Miner

**Pega os drops da Twitch sozinho, sem deixar o navegador aberto assistindo live.**

[![Baixar](https://img.shields.io/github/v/release/brunomazello/twitchdrops?label=baixar&color=9146ff&style=for-the-badge)](https://github.com/brunomazello/twitchdrops/releases/latest)
![Windows](https://img.shields.io/badge/Windows-10%20%7C%2011-0e0e10?style=for-the-badge)
[![Licença MIT](https://img.shields.io/badge/licen%C3%A7a-MIT-00f59b?style=for-the-badge)](LICENSE)

</div>

---

## O que é

Um programa para Windows que "assiste" as lives por você para avançar os drops: troca de canal quando a live cai, resgata o drop quando fica pronto e avisa no canto da tela. Ele não baixa vídeo nem áudio, só os dados que a Twitch usa para contar o tempo, então quase não gasta internet.

É uma versão própria do [Twitch Drops Miner do DevilXD](https://github.com/DevilXD/TwitchDropsMiner), com interface nova, em português, acesso pelo celular e atualização automática.

## Baixar e usar

1. Baixe o **`TwitchDropsMiner.zip`** da [última versão](https://github.com/brunomazello/twitchdrops/releases/latest) e extraia numa pasta só dele (ex.: `Documentos\TwitchDropsMiner`).
2. Abra o `TwitchDropsMiner.exe`. Não precisa instalar nada.
   > Se aparecer *"O Windows protegeu o computador"*, clique em **Mais informações → Executar assim mesmo**. Isso acontece porque o programa não tem assinatura digital paga.
3. Clique em **Entrar com a Twitch** e faça login.
4. **Vincule a conta do jogo** na [página de campanhas da Twitch](https://www.twitch.tv/drops/campaigns). Sem isso a Twitch não libera o drop; o programa avisa quando falta vincular.
5. Em **Configurações → Jogos prioritários**, adicione os jogos que quer e clique em **Recarregar agora**.

Pronto. Pode minimizar: ele fica perto do relógio e avisa quando resgatar um drop.

## O que ele faz

| | |
|---|---|
| 🎁 **Mineração sem vídeo** | Avança os drops sem baixar a live. Resgata sozinho quando fica pronto. |
| 🔀 **Troca de canal automática** | Muda de canal quando a live cai ou quando entra um jogo com prioridade maior. |
| 📋 **Prioridade e exclusão** | Escolha quais jogos minerar, em que ordem, e quais ignorar. |
| 🔔 **Avisos do Windows** | Notificação quando resgata um drop ou quando uma campanha precisa de vínculo. |
| 🗕 **Fica na bandeja** | Minimizar esconde perto do relógio. Dá para iniciar junto com o Windows. |
| 📱 **Acesso pelo celular** | Acompanhe o progresso, troque de canal e mude a prioridade pelo celular, com app instalável. |
| 🌍 **Fora de casa** | Com o [Tailscale](https://tailscale.com/), o celular acessa o PC de qualquer lugar. |
| ⬆️ **Atualização automática** | Baixa e instala versões novas sozinho, sem perder login nem configurações. |

## Acesso pelo celular

Em **Configurações → Celular**, ligue o acesso e leia o QR code com o celular (mesmo Wi-Fi do PC). No Android, use **Adicionar à tela inicial** para ter um app.

**Fora de casa:** instale o Tailscale no PC e no celular, entre com a mesma conta nos dois e clique em **Verificar** na seção *Fora de casa*. Aparece um segundo QR code que funciona de qualquer lugar.

> [!TIP]
> Se o celular conecta no Tailscale mas a página não abre, é o Firewall do Windows. Rode no **PowerShell como administrador**:
> ```powershell
> New-NetFirewallRule -DisplayName "Twitch Drops Miner (Tailscale)" -Direction Inbound -Action Allow -Protocol TCP -LocalPort 8642 -RemoteAddress 100.64.0.0/10 -Profile Any
> ```

## Avisos importantes

> [!WARNING]
> **Não assista live na mesma conta enquanto o programa minera.** A Twitch se confunde e o progresso pode travar.

> [!CAUTION]
> O arquivo **`cookies.jar`** guarda o seu login. Quem tiver esse arquivo entra na sua conta da Twitch sem senha. **Nunca mande a sua pasta do programa para ninguém**: mande o link da versão.

> [!IMPORTANT]
> Automatizar a visualização de lives não é algo que a Twitch apoia. Use por sua conta e risco.

## Dicas e problemas comuns

- **Notificação não aparece durante o jogo:** é o *Não perturbe* do Windows 11. Desligue em *Configurações → Sistema → Notificações → Ativar o "não perturbe" automaticamente → Ao jogar um jogo*.
- **Pede para instalar o "Microsoft Edge WebView2":** aceite. É um componente gratuito da Microsoft que desenha a interface (já vem no Windows 11).
- **Antivírus acusou o `.exe`:** falso positivo comum em programas feitos com PyInstaller. Se preferir, rode pelo código-fonte (abaixo).
- **Login, configurações e cache** ficam na mesma pasta do `.exe`. Se mudar o programa de pasta, desligue e ligue de novo o *Iniciar com o Windows*.

---

## Para desenvolvedores

<details>
<summary><b>Rodar pelo código-fonte</b></summary>

Requer Python 3.10+ e Git.

```bat
setup_env.bat
run_dev.bat
```

`setup_env.bat` cria o ambiente `env\` e instala o `requirements.txt`. Para abrir a interface com as ferramentas de desenvolvedor do navegador, defina `TDM_UI_DEBUG=1` antes de rodar.

</details>

<details>
<summary><b>Como está organizado</b></summary>

| Arquivo | O que faz |
|---|---|
| `main.py` | Inicialização, janela (pywebview + Edge WebView2) e bloqueio de instância única |
| `twitch.py`, `inventory.py`, `channel.py`, `websocket.py` | Lógica de mineração herdada do projeto original |
| `gui.py` | Ponte entre o minerador e a interface; bandeja e notificações |
| `web/` | Interface do PC (`index.html`, `app.css`, `app.js`) e página do celular (`mobile.html`) |
| `remote.py` | Servidor do celular (aiohttp) e integração com o Tailscale |
| `updater.py` | Atualização automática pelos Releases do GitHub |

</details>

<details>
<summary><b>Publicar uma versão nova</b></summary>

Com tudo commitado, rode:

```bat
release.bat 1.0.2
```

Ele atualiza `version.py`, cria o commit e a tag `v1.0.2` e envia. O GitHub Actions ([`release.yml`](.github/workflows/release.yml)) compila o `.exe` e publica o Release; as cópias instaladas se atualizam sozinhas em até 6 horas (ou na hora, em *Configurações → Atualizações → Procurar agora*).

</details>

## Créditos

Baseado no [Twitch Drops Miner](https://github.com/DevilXD/TwitchDropsMiner), de **DevilXD**, sob a [licença MIT](LICENSE). Se o programa te ajudou, considere apoiar o autor original no [Buy Me a Coffee](https://www.buymeacoffee.com/DevilXD) ou no [Patreon](https://www.patreon.com/bePatron?u=26937862).

<details>
<summary>Colaboradores e tradutores do projeto original</summary>

@guihkx - For the CI script, CI maintenance, and everything related to Linux builds.  
@kWAYTV - For the implementation of the dark mode theme.  
@crocchetto - For the macOS port.  

@Bamboozul - For the entirety of the Arabic (العربية) translation.  
@Suz1e - For the entirety of the Chinese (简体中文) translation and revisions.  
@wwj010, @zhangminghao1989, @Self4215 - For the Chinese (简体中文) translation corrections and revisions.  
@Ricky103403 - For the entirety of the Traditional Chinese (繁體中文) translation.  
@LusTerCsI - For the Traditional Chinese (繁體中文) translation corrections and revisions.  
@nwvh - For the entirety of the Czech (Čeština) translation.  
@Kjerne - For the entirety of the Danish (Dansk) translation.  
@lmdpocus - For the entirety of the Dutch (Nederlandse) translation.  
@Rensoraa - For the Traditional Dutch (Nederlandse) translation corrections and revisions.  
@roobini-gamer - For the entirety of the French (Français) translation.  
@Calvineries - For the French (Français) translation revisions.  
@ThisIsCyreX - For the entirety of the German (Deutsch) translation.  
@Nagyhoho1234 - For the entirety of the Hungarian (Magyar) translation.  
@Eriza-Z - For the entirety of the Indonesian translation.  
@casungo - For the entirety of the Italian (Italiano) translation.  
@ShimadaNanaki - For the entirety of the Japanese (日本語) translation.  
@biroman -  For the entirety of the Norwegian (Norsk) translation.  
@Patriot99 - For the Polish (Polski) translation and revisions (co-authored with @DevilXD).  
@zarigata - For the entirety of the Portuguese (Português) translation.  
@Sergo1217 - For the entirety of the Russian (Русский) translation.  
@kilroy98, @flamesv - For the Russian (Русский) translation corrections and revisions.  
@Shofuu - For the entirety of the Spanish (Español) translation and revisions.  
@Forero-0 - For the Spanish (Español) translation revisions.  
@alikdb - For the entirety of the Turkish (Türkçe) translation.  
@DogancanYr, @Elderly-Emre, @Hweord - For the Turkish (Türkçe) translation corrections and revisions.  
@Nollasko - For the entirety of the Ukrainian (Українська) translation and revisions.  
@kilroy98 - For the Ukrainian (Українська) translation corrections and revisions.  

</details>
