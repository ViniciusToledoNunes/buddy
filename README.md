# Buddy - Meeting Copilot

[![CI](https://github.com/ViniciusToledoNunes/buddy/actions/workflows/ci.yml/badge.svg)](https://github.com/ViniciusToledoNunes/buddy/actions/workflows/ci.yml)

Buddy é um copiloto de reuniões multiplataforma, ativado explicitamente, que transcreve áudio do sistema e microfone em fluxos separados e conecta a conversa ao contexto real dos seus projetos por meio de Codex ou Claude.

Ele funciona localmente por padrão, identifica as fontes como `ME` e `REMOTE`, salva a transcrição incrementalmente e mantém captura, ASR, armazenamento, interface e LLM isolados para que uma falha não derrube toda a sessão.

## O que o Buddy faz hoje

Há dois modos de uso.

- **Modo escuta (`buddy listen`)** — o recomendado. O microfone fica atento a "Hey Buddy"; reuniões são gravadas
  quando você pede; uma sessão do Claude Code que você já tem aberta recebe os eventos e age. Sem tela própria.
- **Modo sessão (`buddy start`)** — uma reunião por vez, com TUI no terminal e o Claude chamado em modo headless.

- Captura separada do microfone (`ME`) e do áudio reproduzido pelo computador (`REMOTE`).
- Transcrição **sempre local** com `faster-whisper`/CTranslate2 em CPU ou GPU compatível. Não existe modo de
  transcrição em nuvem: o áudio nunca sai da máquina.
- **O seu Claude Code é o cérebro.** A cada fala nova, o Buddy chama o Claude Code em modo headless no diretório
  do seu trabalho. Ele chega sabendo quem você é e como os seus sistemas funcionam — pelo `CLAUDE.md` e pela
  memória que já usa no dia a dia — e pesquisa Jira, Slack, git, BigQuery e Datadog quando a conversa pede.
  Roda na sua assinatura, não em crédito de API.
- **Painel contínuo.** Cada resposta substitui o conjunto inteiro de sugestões, em vez de empilhar; quando o
  assunto se resolve, o painel é esvaziado. Tudo fica também em `suggestions.md`, para ler depois.
- **Uma sessão por reunião.** O Claude lembra o que já pesquisou minutos antes e recebe só as falas novas; o
  contexto compartilhado vem do cache.
- **Investigação manual via Codex/Claude.** A skill `meeting-copilot-context` e o MCP continuam disponíveis
  para quando você quiser perguntar diretamente.
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

## Modo escuta

```sh
buddy listen --detach      # microfone atento a "Hey Buddy", em segundo plano
buddy status               # listening, meeting ou paused
buddy listen --stop
```

Depois, na sessão do Claude Code que você deixa aberta (a mesma de outros monitores, como o do Slack):

```text
Liga o Buddy.
```

A skill `buddy-listener` arma um `Monitor` sobre `buddy watch --follow --as claude-code` e passa a tratar os
eventos. A partir daí, fale:

| Você diz | Quem trata | O que acontece |
|---|---|---|
| "Hey Buddy, the meeting is starting" | o Buddy, em ~2s | passa a gravar microfone e áudio do sistema |
| "Hey Buddy, the meeting is over" | o Buddy | encerra; o Claude escreve o resumo e as pendências |
| "Hey Buddy, stop listening" | o Buddy | fecha o microfone até `buddy resume` |
| "Hey Buddy, review PR 123" | o Claude | investiga e responde na sessão |
| "Hey Buddy, post the update on Slack" | o Claude | escreve o rascunho e espera você aprovar o texto |
| "Hey Buddy, approve 4" | o Claude | executa a proposta 4 exatamente como proposta |

Um "Hey Buddy" sozinho arma a frase seguinte, dita em até 6 segundos. Comandos são em inglês: o ASR usa um modelo
só-inglês, mais rápido e preciso nas reuniões.

**O que é guardado.** Fala que não começa com "Hey Buddy" e não faz parte de uma reunião é transcrita em memória
e descartada. Reuniões são gravadas por inteiro — cerca de 44 KB por hora de texto. O áudio do sistema só é
capturado durante reunião.

**Durante a reunião,** a sessão recebe um lote de falas nas pausas, no máximo um por minuto: uma notificação por
fala faria o `Monitor` ser interrompido por excesso de eventos, e o Claude leva de 40 a 90 segundos por turno.

**Segurança.** Só o microfone gera comandos: alguém na chamada dizendo "hey buddy, merge it" fica gravado, nunca
é obedecido. O que sai da máquina — postar, comentar, aprovar, mergear, criar ticket — sempre passa por um
rascunho que você aprova. A transcrição é tratada como dado, não como instrução.

**Reunião esquecida.** Termina sozinha após 10 minutos sem fala ou 4 horas de duração
(`listen.meeting_idle_minutes`, `listen.meeting_max_minutes`).

**Vários leitores.** Cada `buddy watch --as <nome>` tem seu próprio cursor em disco. Uma segunda sessão não
consome os eventos da primeira, e um monitor que expirou recebe, ao ser religado, o que aconteceu no intervalo.
`--peek` olha sem avançar.

Outros controles: `buddy meeting start|stop`, `buddy pause`, `buddy resume`. O servidor MCP (`meeting_status`,
`get_live_transcript`, `stop_meeting`) enxerga as reuniões abertas pelo modo escuta.

## Durante a reunião (modo sessão)

- `Ctrl+Alt+Space`: pede uma atualização do painel agora. Se o Claude já estiver trabalhando, o pedido entra na
  fila em vez de interromper — a pesquisa em curso não é jogada fora.
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
buddy tool --list          # conectores somente leitura que o Claude usa (BigQuery, Datadog, Jira)
buddy watch --as nome     # eventos do modo escuta desde a última leitura deste nome
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
transcript.txt      transcrição legível, gravada a cada fala
transcript.jsonl    a mesma, estruturada
suggestions.md      tudo que o painel sugeriu, com o motivo
summary.md          relatório final
metadata.json       início, fim, backend de ASR e falhas de subsistema
copilot.json        memória, sugestões atuais, uso, último erro, chamadas bloqueadas e id da sessão do Claude
```

O `copilot.json` guarda o `brain_session_id`. Depois da reunião, no diretório configurado em `claude_workdir`,
`claude --resume <id>` abre a mesma conversa — com tudo que o Claude leu e pesquisou durante a reunião.

Com `save_audio: false`, padrão do projeto, o áudio é descartado após o processamento. Quando habilitado, são criados `audio_me.wav` e `audio_remote.wav`.

## O cérebro

Com `llm_provider: claude-code`, cada análise é uma execução do Claude Code headless:

| Decisão | Por quê |
|---|---|
| roda em `claude_workdir` | é o diretório cujo `CLAUDE.md` descreve o seu trabalho; o Buddy não tem esse conhecimento |
| `--setting-sources project` | não herda as regras de *allow* do usuário, que costumam liberar `git push` |
| `--permission-mode dontAsk` | ninguém está olhando para aprovar; o que não está liberado é negado |
| `claude_allowed_tools` / `claude_disallowed_tools` | só leitura; verbos de escrita dos seus helpers bloqueados explicitamente |
| sem `ANTHROPIC_API_KEY` no ambiente | a chave faria o Claude Code cobrar a API em vez da assinatura |
| `claude.exe` nativo no Windows | o shim `.cmd` do npm corta o prompt de sistema na primeira quebra de linha |
| `--session-id` e depois `--resume` | uma conversa por reunião; do segundo turno em diante o contexto vem do cache |

A transcrição é tratada como dado, nunca como instrução: se alguém na reunião pedir uma ação, o Claude no máximo
sugere que você a faça.

Chamadas negadas aparecem no STATUS como `tools: denied` e ficam em `copilot.json`, para você decidir se amplia a
regra ou se ela deve continuar bloqueada. Duas falhas seguidas do cérebro ficam vermelhas e o motivo é gravado —
uma semana de reuniões sem sugestão passou despercebida justamente por falta disso.

**BigQuery e Datadog** chegam ao Claude pelo `buddy tool`, que aplica as proteções em código: apenas `SELECT`,
dry-run obrigatório, recusa acima de `bigquery_max_scan_gb` e `--maximum_bytes_billed`. `buddy tool --list` mostra
o que existe. **Jira e Slack** usam os seus próprios helpers, liberados só nos subcomandos de leitura.

Credenciais que vivem em arquivo e nunca são exportadas entram por `env_files` no `config.yaml`.

### Latência e consumo

Medido numa conversa de trabalho: **86s** no primeiro turno (partida fria, com consulta ao Datadog) e **43s** no
seguinte (Jira e git). Serve para posicionamento e respostas a perguntas que ficam no ar; não é resposta
instantânea. `claude_model: sonnet` ou `claude_effort: medium` reduzem o tempo.

Nada é cobrado por token, mas cada execução conta para os limites da sua assinatura. O `usage` em `copilot.json`
traz uma estimativa em dólares (`cost_usd_estimate`) para você acompanhar: no teste, $0,37 no primeiro turno e
$0,10 nos seguintes. O Claude só roda quando há fala nova, então silêncio não consome nada.

### Outros provedores

`openai`, `anthropic` e `ollama` continuam disponíveis, com o motor antigo: contexto pré-carregado, índice de
projeto e investigador em segundo plano. Nenhum deles conhece você, e o `auto` nunca escolhe o `claude-code` —
gastar a assinatura é uma escolha explícita.

## Provedores e privacidade

O áudio nunca sai da máquina: a transcrição é sempre local e não existe modo de ASR em nuvem.

O que chega ao modelo é texto: a transcrição recente, a memória da reunião e aquilo que o próprio Claude decidir
ler com as ferramentas liberadas. Com `claude-code`, isso vai para a Anthropic pela sua conta do Claude Code, sob as
mesmas regras do seu uso normal.

Iniciar por MCP exige `BUDDY_ALLOW_MCP_START=true` e `confirmed=true` na chamada. Verifique consentimento dos
participantes, legislação local e políticas corporativas antes de gravar.

Os nomes antigos das variáveis `MEETING_AGENT_*` e `MEETING_COPILOT_ALLOW_MCP_START` continuam aceitos.

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
- Cada atualização leva de 40 a 90 segundos com o Claude Code. O painel acompanha a reunião, mas não responde
  na hora a uma pergunta recém-feita.
- Regras de permissão casam por prefixo do comando. Se o Claude invocar um helper de um jeito diferente do
  previsto (`sh ~/bin/jira.sh` e não `~/bin/jira.sh`), a chamada é negada e aparece em `tool_denials`.
- No modo sessão, uma sessão esquecida aberta enquanto o computador toca áudio continua transcrevendo e
  chamando o Claude. O modo escuta encerra reuniões sozinho.
- No modo escuta, a detecção de "Hey Buddy" transcreve toda fala do microfone para decidir, ainda que
  descarte o resto. Um detector dedicado de palavra de ativação evitaria isso e gastaria menos CPU (hoje
  ~8% em repouso).
- O `Monitor` pertence à sessão que o criou e expira a cada 30 minutos; a skill o religa. Com a sessão
  fechada, os eventos esperam em disco.

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
