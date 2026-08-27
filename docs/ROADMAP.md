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

## Próximos passos recomendados

### 1. Validar e empacotar cada plataforma

- Testes end-to-end com áudio real em hardware Linux e macOS. A CI cobre lógica e importação, não captura.
- Instaladores assinados e releases versionadas.
- Aplicativo de bandeja com indicador inequívoco de gravação e do conjunto de sugestões atual.

### 2. Entender participantes, não apenas fontes

- Diarização dos participantes remotos.
- Associação opcional entre voz e nome, sempre confirmada pelo usuário.
- Detecção de sobreposição de fala e melhor separação de vazamento acústico.

### 3. Aumentar o contexto útil

- Evoluir a memória entre reuniões de sobreposição de termos para embeddings, reconhecendo sinônimos e paráfrases.
- Criptografia em repouso para a memória estruturada.
- Indexação opt-in de projetos e documentação autorizada.
- Preparação pré-reunião baseada em agenda, participantes e reuniões anteriores.
- Recuperação automática de tickets, pull requests e decisões relacionadas.

### 4. Fechar o ciclo de execução

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
