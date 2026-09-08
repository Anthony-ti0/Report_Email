# **MEF - Validação de Recepção de Remessas**
## Descrição

Automação responsável por validar os arquivos recebidos de fornecedores, comparando-os com os registros de importação armazenados no banco de dados. O processo identifica arquivos importados e pendentes, gera uma evidência da validação e envia notificações por e-mail e Google Chat.

## Objetivo

Garantir que os arquivos recebidos durante o processo de remessa sejam devidamente importados, proporcionando maior controle, rastreabilidade e agilidade na identificação de arquivos pendentes.

## Fluxo do processo
1. Identifica a pasta de remessas referente à data atual.
2. Realiza uma busca recursiva pelos arquivos recebidos, incluindo arquivos armazenados em subpastas.
3. Consulta o banco de dados para obter os arquivos importados no dia.
4. Compara os arquivos recebidos com os registros de importação.
5. Classifica os arquivos entre:
- Importados
- Pendentes
6. Caso existam arquivos pendentes, o processo pode aguardar uma nova execução ou enviar um alerta ao Google Chat ao final da janela de processamento.
7. Quando todos os arquivos são importados:
- Gera um relatório visual em formato PNG;
- Envia um e-mail com o resultado da validação e a evidência;
- Envia uma notificação ao Google Chat;
- Registra que o relatório do dia já foi enviado, evitando duplicidade.

## Funcionalidades
- Validação automática dos arquivos recebidos.
- Consulta de registros de importação no banco de dados.
- Comparação entre arquivos recebidos e importados.
- Identificação de arquivos pendentes.
- Busca recursiva em subpastas.
- Geração de relatório visual da validação.
- Envio de relatório por e-mail.
- Envio de notificações pelo Google Chat.
- Controle para evitar o envio duplicado do relatório diário.
- Execução forçada através do parâmetro ```--force```.

## Tecnologias utilizadas
- Python
- Pandas
- PyODBC
- Matplotlib
- SQL Server
- SMTP
- Google Chat Webhook

## Estrutura do processo

```text
Arquivos recebidos
        │
        ▼
Leitura da pasta de remessas
        │
        ▼
Consulta ao banco de dados
(registros de importação)
        │
        ▼
Reconciliação dos arquivos
        │
        ├── Arquivos pendentes
        │       │
        │       ▼
        │   Alerta no Google Chat
        │       │
        │       ▼
        │   Nova checagem
        │
        └── Todos os arquivos importados
                │
                ▼
        Geração da evidência
                │
                ├── Relatório por E-mail
                │
                └── Notificação no Google Chat
```

O projeto utiliza variáveis de ambiente para configuração dos caminhos, banco de dados, SMTP e Google Chat.

Principais variáveis utilizadas:
| Variável | Descrição |
|---|---|
| `MEF_DRIVE_REMESSAS_PATH` | Caminho base das remessas |
| `MEF_VALIDATION_MARKER_PATH` | Arquivo utilizado para controlar o envio diário |
| `MEF_VALIDATION_PNG_PATH` | Caminho para salvar a evidência em PNG |
| `MEF_SMTP_HOST` | Servidor SMTP |
| `MEF_SMTP_PORT` | Porta do servidor SMTP |
| `MEF_SMTP_USER` | Usuário do SMTP |
| `MEF_SMTP_PASSWORD` | Senha do SMTP |
| `MEF_VALIDATION_EMAIL_TO` | Destinatário do relatório |
| `MEF_GCHAT_WEBHOOK_URL` | Webhook do Google Chat |
| `MEF_GCHAT_MENTION_USER_IDS` | Usuários mencionados nas notificações |
A string de conexão com o banco de dados é obtida através da função ```get_connection_string()``` disponibilizada pelo módulo **report_automation**.

Execução

Execução normal:
```
python <nome_do_script>.py
```
Execução forçada:
```
python <nome_do_script>.py --force
```
O parâmetro ```--force``` permite que o processo avance para a etapa de notificação mesmo quando a validação ainda não estiver concluída.

Resultado

Ao concluir a validação, o sistema disponibiliza uma evidência contendo:
- Nome dos arquivos recebidos;
- Data e hora do processamento;
- Data e hora de término da importação;
- Identificação de arquivos ainda não importados.

Quando todos os arquivos são processados corretamente, o resultado é comunicado por e-mail e Google Chat.
