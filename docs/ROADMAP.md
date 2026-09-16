# Roadmap de capacidades do Buddy

O Buddy já cobre captura, transcrição, sugestões contínuas, memória dentro e entre reuniões, relatórios e conexão
com projetos via MCP/skill. As evoluções abaixo ampliariam o produto sem alterar a regra central de consentimento
explícito.

## Entregue

- **Sugestões contínuas.** Qualquer fala nova reavalia o conjunto inteiro e substitui o painel; um conjunto vazio
  limpa conselhos que deixaram de ser úteis. Não há mais gatilho por palavra-chave.
- **Memória entre reuniões.** `find_related_meetings` classifica reuniões anteriores por sobreposição de termos
  sobre memória estruturada, disponível para o copiloto durante a reunião e para Codex/Claude via MCP.
- **CI multiplataforma.** Testes com cobertura em `ubuntu-latest` (3.12 e 3.13), `windows-latest` e `macos-15`,
  incluindo verificação de sintaxe do helper Swift e checagem de importação por plataforma.

- **O Claude Code do usuário como cérebro.** O motor interno sugeria sem contexto: o "projeto" que ele recebia era
  o próprio código do Buddy, e o modelo não sabia quem era o usuário. Agora cada análise é uma execução headless do
  Claude Code no diretório de trabalho, com o `CLAUDE.md`, a memória e os helpers que o usuário já usa, numa sessão
  por reunião, somente leitura e isolada das regras de permissão do usuário. BigQuery e Datadog continuam
  protegidos em código, acessados pelo `buddy tool`.

- **Modo escuta.** `buddy listen` mantém o microfone atento a "Hey Buddy", grava reuniões quando pedido e
  publica eventos num log em disco; a skill `buddy-listener` faz de uma sessão do Claude Code o cérebro, com o
  mesmo padrão de um monitor de Slack: cursor em disco, `Monitor`, investigação livre e escrita só com aprovação.

## Próximos passos recomendados

### 0. Modo escuta: próximos passos

- Detectar reunião pelo sinal do sistema: o Windows informa quais apps estão usando o microfone (Zoom, Chrome,
  Teams). Perguntar se deve gravar quando uma chamada começa sem aviso, e encerrar quando o app solta o
  microfone. Precisa de lista de apps: um cliente de VDI pode segurar o microfone o dia todo.
- Detector dedicado de palavra de ativação, para não transcrever fala que será descartada.
- Validar "Hey Buddy" em reuniões reais: taxa de disparo falso e de comando perdido.

### 1. Reduzir a latência do cérebro

- Hoje leva de 40 a 90s por atualização. Medir `claude_model: sonnet` e `claude_effort: medium` numa reunião real.
- Encerrar sozinho uma sessão esquecida, para ela não continuar chamando o Claude enquanto o computador toca áudio.
- Testar o overlay (`ui.overlay: true`) como tela principal durante chamadas: ele já mostra o conjunto atual,
  mas a TUI fica num terminal atrás da janela da reunião.

## Próximos passos recomendados

### 2. Validar e empacotar cada plataforma

- Testes end-to-end com áudio real em hardware Linux e macOS. A CI cobre lógica e importação, não captura.
- Instaladores assinados e releases versionadas.
- Aplicativo de bandeja com indicador inequívoco de gravação e do conjunto de sugestões atual.

### 3. Entender participantes, não apenas fontes

- Diarização dos participantes remotos.
- Associação opcional entre voz e nome, sempre confirmada pelo usuário.
- Detecção de sobreposição de fala e melhor separação de vazamento acústico.

### 4. Aumentar o contexto útil

- Evoluir a memória entre reuniões e o índice de projeto de BM25 para embeddings, reconhecendo sinônimos e
  paráfrases (hoje `store` não encontra `storage`).
- Criptografia em repouso para a memória estruturada.
- Indexação opt-in de projetos e documentação autorizada.
- Preparação pré-reunião baseada em agenda, participantes e reuniões anteriores.
- Conectores Jira e BigQuery no `ToolRegistry`. BigQuery precisa de read-only, dry-run e limite de bytes por
  query: um agente autônomo consultando dados tem raio de alcance maior que gasto de tokens.

### 5. Fechar o ciclo de execução

- Rascunhos de issues e tarefas para GitHub, Jira, Linear ou Todoist.
- Follow-up por e-mail ou chat sujeito a revisão humana.
- Atualização de documentação e ADRs a partir de decisões aprovadas.
- Rastreamento de responsáveis, prazos e itens concluídos.

## Capacidades de produto futuras

- Aplicativo desktop nativo com histórico, busca e configurações.
- Integração com Zoom, Google Meet, Microsoft Teams e calendários.
- Tradução e legendas bilíngues em tempo real.
- Modo totalmente offline com LLM local e aceleração por GPU/NPU.
- Compartilhamento de reuniões com criptografia, retenção e controles organizacionais.
- Métricas de qualidade da reunião, como decisões sem responsável ou riscos sem plano.
- API local estável e plugins para fluxos de trabalho personalizados.

## Fora do escopo atual

- Gravação oculta ou automática sem consentimento.
- Envio irrestrito de áudio, transcrições ou arquivos para serviços externos.
- Ações externas irreversíveis sem revisão e autorização do usuário.
- Inferência silenciosa da identidade de participantes.
