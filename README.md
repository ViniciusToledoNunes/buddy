# Buddy - Meeting Copilot

[![CI](https://github.com/ViniciusToledoNunes/buddy/actions/workflows/ci.yml/badge.svg)](https://github.com/ViniciusToledoNunes/buddy/actions/workflows/ci.yml)

Buddy é um copiloto de reuniões multiplataforma, ativado explicitamente, que transcreve áudio do sistema e microfone em fluxos separados e conecta a conversa ao contexto real dos seus projetos por meio de Codex ou Claude.

Ele funciona localmente por padrão, identifica as fontes como `ME` e `REMOTE`, salva a transcrição incrementalmente e mantém captura, ASR, armazenamento, interface e LLM isolados para que uma falha não derrube toda a sessão.

## O que o Buddy faz hoje

- Captura separada do microfone (`ME`) e do áudio reproduzido pelo computador (`REMOTE`).
- Transcrição **sempre local** com `faster-whisper`/CTranslate2 em CPU ou GPU compatível. Não existe modo de
  transcrição em nuvem: o áudio nunca sai da máquina.
- **Duas camadas de Claude.** A camada 1 reavalia o painel inteiro a cada poucos segundos com `claude-haiku-4-5`,
  substituindo as sugestões em vez de empilhá-las; quando o assunto se resolve, o painel é esvaziado.
- **Camada 2 sob demanda (`Ctrl+Alt+Space`).** `claude-opus-5` com ferramentas reais lê o repositório — busca,
  leitura de arquivo, histórico do git e memória de reuniões — e responde citando `caminho:linha`.
- **Contexto de projeto no prompt.** Arquivos do repositório são ranqueados por relevância (BM25, com quebra de
  `snake_case` e `camelCase`) e entram no prefixo cacheado, então a camada 1 também cita código sem custo de
  ida e volta de ferramenta.
- Memória entre reuniões: o Buddy recupera reuniões anteriores relacionadas usando apenas memória estruturada
  (resumo, tópicos, decisões, ações, perguntas). Transcrições brutas nunca são indexadas nem enviadas ao provedor.
- Memória compacta da reunião atual com decisões, ações e perguntas em aberto.
- Relatório final em Markdown e transcrição incremental em texto e JSONL.
- TUI em tempo real, overlay opcional e atalhos globais.
- Benchmark local para escolher modelo e backend de ASR adequados à máquina.
- Servidor MCP local para consultar status, transcrição ao vivo, reuniões salvas, buscas, contexto do copiloto e
  reuniões anteriores relacionadas (`find_related_meetings`).
- Skill `meeting-copilot-context` para Codex e Claude relacionarem a reunião com código, documentação, git e pesquisa externa.
- Controles seguros: começa parado, `start` via MCP fica desabilitado por padrão e caminhos fora de `meetings_dir` são rejeitados.

## Compatibilidade

| Plataforma | Áudio do sistema | Microfone | Estado |
|---|---|---|---|
| Windows 10/11 | WASAPI loopback | WASAPI | Validado em hardware real |
| Linux desktop | PipeWire sink capture | PipeWire source | Implementado; requer validação no hardware alvo |
| macOS 15+ | ScreenCaptureKit | ScreenCaptureKit | Implementado; requer validação no hardware alvo |

O repositório contém código, dependências Python, instaladores, configuração de exemplo, skill, servidor MCP, testes e documentação. Ele não contém nem deve conter:

- chaves de API ou `.env` pessoal;
- gravações e reuniões salvas;
- modelos de ASR, que são baixados no primeiro uso;
- FFmpeg, PipeWire, Python ou Xcode Command Line Tools, que são dependências do sistema;
- identificadores de dispositivos de outra máquina.

Portanto, o Buddy pode ser instalado em qualquer máquina **dentro da matriz suportada**, desde que os pré-requisitos do sistema sejam atendidos. Consulte [portabilidade e pré-requisitos](docs/PORTABILITY.md) antes de migrar.

## Instalação rápida

Clone o projeto:

```sh
git clone https://github.com/ViniciusToledoNunes/buddy.git
cd buddy
```

Windows PowerShell:

```powershell
.\scripts\install-windows.ps1
.\.venv\Scripts\Activate.ps1
buddy doctor
buddy devices
buddy start
```

Linux:

```sh
./scripts/install-linux.sh
. .venv/bin/activate
buddy doctor
buddy devices
buddy start
```

macOS 15+:

```sh
./scripts/install-macos.sh
. .venv/bin/activate
buddy doctor
buddy devices
buddy start
```

Os comandos antigos `meeting-agent` e `meeting-agent-mcp` continuam disponíveis como aliases de compatibilidade.

## Durante a reunião

- `Ctrl+Alt+Space`: dispara a análise profunda, em que o Claude lê o projeto antes de responder. O resultado
  aparece no painel `DEEP ANALYSIS` e é gravado em `analysis.md` na pasta da reunião.
- `Ctrl+Alt+M`: encerra a sessão.
- `buddy stop`: encerra a sessão a partir de outro terminal.
- `buddy status`: informa se existe captura ativa.

Outros comandos:

```sh
buddy benchmark
buddy config
buddy devices
buddy doctor
buddy hotkeys
```

As sugestões aparecem na TUI. Com `ui.overlay: true`, também aparecem em uma janela sempre visível. Sugestões produzidas pela skill aparecem na conversa do Codex ou Claude.

## Codex e Claude

Os instaladores copiam a skill para `~/.codex/skills/meeting-copilot-context` e `~/.claude/skills/meeting-copilot-context`, quando os clientes estão disponíveis, e registram o MCP local com o nome `buddy`.

Depois da instalação, reinicie o cliente e experimente:

```text
Use $meeting-copilot-context para relacionar os últimos cinco minutos da reunião com este projeto.
Use $meeting-copilot-context para verificar no código o risco que acabou de ser mencionado.
Use $meeting-copilot-context para transformar a última reunião em próximos passos do projeto.
```

O MCP fornece somente contexto de reunião com limites definidos. A leitura do projeto é feita pelas ferramentas nativas do agente, mantendo uma fronteira clara de acesso.

## Arquitetura

```text
System audio -> platform capture -> bounded queue --+
                                                   +-> ASR -> event bus -> TUI / overlay
Microphone  -> platform capture -> bounded queue --+                 +-> incremental storage
                                                                     +-> triggers / memory -> LLM

Saved meetings <-> local MCP <-> Codex or Claude <-> current project / docs / git / web
```

Backends de captura:

- Windows: WASAPI loopback via `soundcard`.
- Linux: PipeWire via `pw-record`/`pw-cat`.
- macOS: helper Swift compilado localmente usando ScreenCaptureKit.

## Arquivos gerados

Cada sessão fica em `meetings/YYYY-MM-DD_HHMMSS/`:

```text
transcript.txt
transcript.jsonl
summary.md
metadata.json
copilot.json
```

Com `save_audio: false`, padrão do projeto, o áudio é descartado após o processamento. Quando habilitado, são criados `audio_me.wav` e `audio_remote.wav`.

## Modelos

| Camada | Quando roda | Modelo | Papel |
|---|---|---|---|
| 1 | a cada poucos segundos | `claude-haiku-4-5` | mantém o painel vivo; recebe transcrição, memória e trechos de código |
| 2 | `Ctrl+Alt+Space` | `claude-opus-5` | lê o repositório com ferramentas e responde citando `caminho:linha` |

O prefixo do prompt — instruções mais trechos de projeto — é marcado para *prompt caching*, porque é a parte que
não muda durante a reunião. Leituras de cache custam cerca de um décimo da entrada normal, que é o que torna um
refresh a cada poucos segundos viável.

Ollama continua disponível com `llm_provider: ollama` para operação totalmente offline.

## Provedores e privacidade

Copie `.env.example` para `.env` e preencha somente os provedores desejados. OpenAI, Anthropic e Ollama são opcionais; a transcrição local funciona sem chave.

O simples fato de existir uma chave não permite upload de áudio. Para usar ASR em nuvem são necessárias as duas configurações:

```yaml
asr_mode: cloud-fast
```

```dotenv
BUDDY_ALLOW_CLOUD_AUDIO=true
```

Iniciar por MCP também exige `BUDDY_ALLOW_MCP_START=true` e `confirmed=true` na chamada. Verifique consentimento dos participantes, legislação local e políticas corporativas antes de gravar ou transmitir conteúdo.

Os nomes antigos das variáveis `MEETING_AGENT_*` e `MEETING_COPILOT_ALLOW_MCP_START` continuam aceitos para compatibilidade.

## Resultado nesta máquina

Validado em 2026-08-26 no Windows 11 Pro, Intel Core i5-1135G7, 16 GB de RAM, Intel Iris Xe e sem CUDA. O backend selecionado foi `faster-whisper base.en`, CPU `int8`, com RTF aproximado de `0.036` para uma amostra de 60 segundos e latência final observada de aproximadamente 1,4 a 1,7 segundo após o fim da fala.

Resultados variam por máquina. Execute `buddy benchmark` para medir o ambiente alvo.

## Limitações atuais

- `ME` e `REMOTE` representam fontes físicas, não pessoas individuais.
- Não há diarização ou identificação de cada participante remoto.
- Linux e macOS rodam na CI, mas ainda precisam de teste end-to-end com áudio real antes de serem considerados
  validados. Captura, ASR e o helper Swift dependem de hardware e não entram no gate automatizado.
- O Buddy ainda não possui aplicativo desktop completo, bandeja do sistema ou instalador assinado.
- Integrações com calendário, plataformas de reunião, Jira, GitHub ou CRM ainda não são automáticas.
- A memória entre reuniões e o índice de projeto usam ranqueamento léxico (BM25), não embeddings; sinônimos e
  paráfrases ainda não são reconhecidos. `store` não encontra `storage`.
- Trechos de código entram no prompt da camada 1. Arquivos de credencial são excluídos por construção
  (`.env*`, `*.pem`, `*.key`, nomes com `password`/`credential`), mas se o seu código-fonte contém segredos em
  texto puro, desligue com `project_context_enabled: false`.

Veja o [roadmap de capacidades](docs/ROADMAP.md) para as próximas evoluções possíveis.

## Desenvolvimento

```sh
python -m venv .venv
# Windows: .\.venv\Scripts\python -m pip install -e ".[dev]"
# Linux/macOS: .venv/bin/python -m pip install -e ".[dev]"
pytest -q --cov --cov-fail-under=80
```

O projeto requer Python 3.12, 3.13 ou 3.14.

A CI roda em `ubuntu-latest` (3.12 e 3.13), `windows-latest` e `macos-15`. Ela executa os testes com cobertura,
verifica que todo módulo compila e que os módulos da plataforma atual importam, valida a sintaxe do helper Swift em
`native/macos/MeetingAudioCapture.swift` e confirma que a CLI inicia.

O gate de cobertura de 80% cobre o núcleo independente de hardware: copiloto, memória, MCP, TUI, overlay,
armazenamento, configuração e seleção de ASR. Captura de áudio, ASR, benchmark, `doctor`, CLI e sessão dependem de
dispositivos reais e são verificados por `buddy doctor` e pela checklist em [docs/PORTABILITY.md](docs/PORTABILITY.md).

Antes de publicar alterações, execute os testes e valide a skill com o script `quick_validate.py` do `skill-creator`.
