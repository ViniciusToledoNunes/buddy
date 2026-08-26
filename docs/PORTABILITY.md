# Portabilidade do Buddy

Este documento descreve o que uma máquina nova precisa para executar o Buddy - Meeting Copilot.

## Requisitos compartilhados

- Python 3.12, 3.13 ou 3.14.
- Git para clonar e atualizar o repositório.
- Internet durante a primeira instalação e o primeiro download do modelo local.
- Aproximadamente 2 GB livres para ambiente Python, caches e um modelo local básico. Modelos maiores exigem mais espaço.
- Um dispositivo de saída e um microfone reconhecidos pelo sistema.
- FFmpeg é recomendado e necessário para o benchmark baseado em arquivos de mídia; a captura ao vivo não depende dele em todos os backends.

Codex, Claude e chaves de API são opcionais. Sem eles, o Buddy ainda pode transcrever localmente e salvar reuniões, mas não terá pesquisa de projeto via skill nem sugestões de um provedor remoto.

## Windows 10/11

Requisitos:

- PowerShell 5.1 ou mais recente.
- Python x64 em uma versão suportada; o instalador tenta 3.14, 3.13 e 3.12 nessa ordem.
- Endpoint WASAPI de saída e microfone.

Instalação e verificação:

```powershell
.\scripts\install-windows.ps1
.\.venv\Scripts\Activate.ps1
buddy doctor
buddy devices
```

O Windows é a plataforma atualmente validada em hardware real.

## Linux desktop

Requisitos:

- PipeWire ativo na sessão do usuário.
- `pw-record` ou `pw-cat` disponível no `PATH`.
- Python e suporte do desktop para os atalhos desejados.

Verificação prévia:

```sh
python3 --version
command -v pw-record || command -v pw-cat
```

Instalação:

```sh
./scripts/install-linux.sh
. .venv/bin/activate
buddy doctor
buddy devices
```

Em Wayland, atalhos globais podem precisar ser configurados no próprio ambiente gráfico. O nó PipeWire de saída também pode precisar ser escolhido explicitamente em configurações incomuns.

## macOS 15 ou mais recente

Requisitos:

- Python em uma versão suportada.
- Xcode Command Line Tools com `swiftc`.
- Permissões de Screen Recording, Microphone e, para atalhos globais, Accessibility.

Instalação:

```sh
xcode-select --install
./scripts/install-macos.sh
. .venv/bin/activate
buddy doctor
buddy devices
```

O instalador compila `native/macos/MeetingAudioCapture.swift` para `.venv/bin/meeting-audio-macos`. O binário não é armazenado no Git porque depende da plataforma alvo.

## O que migrar entre máquinas

Migre:

- o repositório Git;
- alterações próprias em `config.yaml`, revisadas para os novos dispositivos;
- opcionalmente a pasta `meetings/`, se o histórico também precisar viajar.

Não migre pelo repositório:

- `.env` ou chaves de API;
- `.venv`;
- caches e modelos baixados;
- `.meeting-agent`, que contém estado transitório;
- IDs de dispositivo sem validá-los na máquina nova.

## Checklist de aceitação em uma máquina nova

1. O instalador termina sem erros.
2. `buddy doctor` não mostra falha no backend de áudio.
3. `buddy devices` lista saída e microfone esperados.
4. Uma sessão curta distingue `ME` e `REMOTE` e termina com `buddy stop`.
5. `summary.md` e `transcript.jsonl` são criados.
6. Se Codex/Claude estiverem instalados, o MCP `buddy` conecta e `meeting_status` responde.
7. Se nuvem for habilitada, consentimento e opt-in foram verificados antes do teste.
