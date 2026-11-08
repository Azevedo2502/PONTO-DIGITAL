# Ponto STAR

Sistema de registro de ponto com painel administrativo, folha mensal e tela de quiosque.

## Executar localmente

Use Python 3.11 ou superior. Instale as dependências e inicie o servidor:

```powershell
python -m pip install -r requirements.txt
python app.py
```

O servidor local escuta na porta 5000 sem o depurador do Flask. Para abrir a tela em outro aparelho na mesma rede, use o endereço IP do computador. A instalação da Tela de Início do iPad requer HTTPS; use a implantação online descrita abaixo.

O painel bloqueia novas tentativas por 15 minutos após cinco senhas incorretas para a mesma combinação de origem e usuário. Em produção, o app exige HTTPS e marca os cookies de sessão como `Secure`.

## Segurança e chaves

Em produção, defina `APP_ENV=production`, `SECRET_KEY` e `CODIGO_SECRET_KEY` no painel de variáveis do provedor. Gere valores diferentes executando duas vezes:

```powershell
python -c "import secrets; print(secrets.token_hex(32))"
```

Não publique nem envie esses valores por chat. `SECRET_KEY` protege as sessões do administrador. `CODIGO_SECRET_KEY` protege os códigos dos funcionários.

Para uma base criada antes desta alteração, use temporariamente `LEGACY_CODIGO_SECRET_KEY` com a chave que era usada no `db.py`. Quando um funcionário registra o ponto com seu código, o hash é atualizado para `CODIGO_SECRET_KEY`; remova `LEGACY_CODIGO_SECRET_KEY` depois que a migração terminar. Em desenvolvimento, a chave antiga continua sendo usada por padrão para não invalidar os códigos locais.

## Recomendação de hospedagem: Railway

O projeto usa SQLite. Na Railway, crie um volume persistente montado em `/data` para que os dados sobrevivam a novos deploys. Configure estas variáveis no serviço:

| Variável | Valor |
|---|---|
| `APP_ENV` | `production` |
| `SECRET_KEY` | segredo aleatório gerado localmente |
| `CODIGO_SECRET_KEY` | outro segredo aleatório |
| `PONTO_DB_PATH` | `/data/ponto.db` |
| `TRUST_PROXY_COUNT` | `1` |
| `PUBLIC_ORIGIN` | origem HTTPS do serviço, por exemplo `https://seu-dominio.up.railway.app` |
| `LEGACY_CODIGO_SECRET_KEY` | temporário, somente durante a migração de códigos existentes |
| `ADMIN_INICIAL_USUARIO` | temporário, usuário do primeiro admin (3 a 30 caracteres) |
| `ADMIN_INICIAL_SENHA` | temporário, senha do primeiro admin (mínimo de 8 caracteres) |

As duas variáveis `ADMIN_INICIAL_*` são opcionais e só criam o administrador se
a tabela ainda estiver vazia. A senha inicial exige troca no primeiro acesso.
Depois de entrar e definir uma senha permanente, remova essas duas variáveis
temporárias da Railway. Mantenha `SECRET_KEY`, `CODIGO_SECRET_KEY` e
`PONTO_DB_PATH` configuradas.

O `Procfile` inicia Gunicorn com um worker e a porta fornecida pela plataforma. A Railway termina HTTPS na sua borda; o app redireciona tráfego HTTP para HTTPS. Use o domínio HTTPS do serviço ou configure um domínio próprio antes de instalar o app no iPad. O cookie da sessão só é enviado por HTTPS em produção.

O backup pelo painel baixa uma cópia consistente do SQLite. Guarde o arquivo em local protegido: ele contém registros de ponto, dados de funcionários e hashes de credenciais.

Esta pasta ainda precisa ser publicada em um repositório conectado à Railway e configurada com as variáveis e o volume acima para que a implantação fique online; nenhum segredo ou conta de hospedagem deve ser compartilhado no chat.
