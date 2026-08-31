# monitor-pc-pichau

Monitor automático de preços da montagem de PC na Pichau — GitHub Actions + Playwright + Telegram.
Mesma arquitetura do `monitor-resort-latorre`.

Roda **a cada 30 minutos**, lê o preço **à vista no PIX** de cada peça e dispara **uma** mensagem
no Telegram sempre que algum valor muda, para cima ou para baixo. A mensagem traz o novo total
em destaque e a lista do que mudou. Peça que sai ou volta ao estoque também gera alerta.

## Peças monitoradas

Editar a lista `ALVOS` em `monitor_pichau.py`. Cada item é um dicionário com `nome`
(rótulo nas mensagens), `slug` (o caminho do produto no site, o que vem depois de
`pichau.com.br`) e `qtd`.

## Segredos necessários

| Secret | O que é |
|---|---|
| `TELEGRAM_TOKEN` | token do bot no BotFather |
| `CHAT_ID` | id do chat/grupo que recebe os alertas |

Configurar em **Settings → Secrets and variables → Actions**.

## Estado

`estado.json` guarda o último preço, o preço no cartão e a disponibilidade de cada peça,
mais o total. O próprio workflow faz commit dele a cada rodada. Apagar o arquivo reinicia
a linha de base — a execução seguinte manda a mensagem de "monitoramento iniciado".

## Detalhes que custaram caro

- O bloco de preço da Pichau é **renderizado no cliente**: o HTML que volta do servidor não
  tem nenhum valor em reais. Daí o `wait_for_function` esperando o texto `R$` aparecer.
- As classes do MUI mudam de hash a cada build (`mui-1jk88bq-price_vista-...`), então o
  casamento é por sufixo (`[class*="price_vista"]`). Isso também pega os rótulos irmãos
  ("à vista", "no PIX com 15% desconto") — por isso a varredura fica com o primeiro
  candidato que tem um valor em reais de verdade.
- Disponibilidade sai do JSON-LD (`schema.org/Product`), que vem no HTML cru.
- Peça que falha na coleta mantém o último valor conhecido, para o total não dar um salto
  falso e disparar alerta à toa.
