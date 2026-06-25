# Usando este Servidor MCP no Trae

Este servidor MCP expõe o Claude Code CLI (`claude`) como ferramentas (tools) e prompts reutilizáveis para o Trae IDE. O Trae inicia o servidor localmente via transporte STDIO e descobre as ferramentas automaticamente.

## Links

- FastMCP: https://github.com/PrefectHQ/fastmcp
- Documentação do FastMCP: https://gofastmcp.com/
- Protocolo Model Context (MCP): https://modelcontextprotocol.io/
- uv: https://docs.astral.sh/uv/

## 1) Preparar o Ambiente

Este projeto utiliza o `uv` para gerenciar o ambiente virtual e as dependências.

```bash
uv sync
```

O comando `uv sync`:
- Lê os arquivos `pyproject.toml` e `uv.lock`.
- Cria o diretório `.venv/` (se não existir).
- Instala todas as dependências do projeto (`fastmcp`, `pydantic`, `pydantic-settings`, etc.).

## 2) Registrar o Servidor MCP no Trae

O Trae suporta servidores MCP via transporte `stdio`, `SSE` e `Streamable HTTP`. A configuração recomendada é um servidor `stdio` utilizando o `uvx` (o executor de ferramentas que acompanha o `uv`). O próprio Trae inclui e expõe o executável `uvx` em seu PATH, portanto, nenhuma instalação adicional de Python é estritamente necessária.

Adicione o servidor MCP manualmente nas configurações do Trae (`Settings → MCP`) ou crie um arquivo `.trae/mcp.json` na raiz do seu projeto.

### 2.1 Configuração Manual Global (via Configurações do Trae)

Adicione a entrada correspondente no bloco de configuração global do Trae:

```json
{
  "mcpServers": {
    "claude-code-cli-mcp": {
      "command": "uvx",
      "args": [
        "--from",
        "/caminho/para/claude-code-cli-mcp",
        "--with",
        "fastmcp>=3.0.0",
        "fastmcp",
        "run",
        "src/claude_code_mcp/server.py"
      ],
      "cwd": "/caminho/para/claude-code-cli-mcp",
      "env": {
        "CLAUDE_MCP_MODE": "safe",
        "CLAUDE_MCP_ALLOWED_ROOTS": "[\"/caminho/para/seus/projetos\"]",
        "CLAUDE_MCP_FORCE_SANDBOX_IN_SAFE_MODE": "true",
        "START_MCP_TIMEOUT_MS": "30000",
        "RUN_MCP_TIMEOUT_MS": "600000"
      }
    }
  }
}
```

### 2.2 Configuração a Nível de Projeto (.trae/mcp.json)

O Trae pode carregar servidores MCP a partir de um arquivo `.trae/mcp.json` na raiz do workspace. Ative o recurso "Project-level MCP" em `Settings → MCP` no Trae e adicione o arquivo ao repositório:

```json
{
  "mcpServers": {
    "claude-code-cli-mcp": {
      "command": "uvx",
      "args": [
        "--from",
        "${workspaceFolder}/claude-code-cli-mcp",
        "fastmcp",
        "run",
        "claude-code-cli-mcp/src/claude_code_mcp/server.py"
      ],
      "cwd": "${workspaceFolder}",
      "env": {
        "CLAUDE_MCP_MODE": "safe",
        "CLAUDE_MCP_ALLOWED_ROOTS": "[${workspaceFolder}]",
        "CLAUDE_MCP_FORCE_SANDBOX_IN_SAFE_MODE": "true"
      }
    }
  }
}
```

Informações Importantes:
- A documentação oficial do Trae recomenda o uso de `uvx` em vez de `python -m fastmcp.cli` para iniciar o servidor.
- O campo `command` não deve conter espaços. Passe os parâmetros adicionais no array `args`.
- A variável `${workspaceFolder}` é expandida pelo Trae no momento da inicialização e aponta para a raiz do projeto aberto.
- As variáveis `START_MCP_TIMEOUT_MS` e `RUN_MCP_TIMEOUT_MS` são interpretadas pelo Trae para limitar o tempo de inicialização do servidor e de chamadas de ferramentas, respectivamente.
- A variável `CLAUDE_MCP_ALLOWED_ROOTS` deve ser fornecida como uma string JSON contendo um array, e não como valores separados por vírgula.

## 3) Solução de Problemas

### `Unable to handle .../.venv`
Este erro geralmente ocorre quando a descoberta de ambiente Python do Trae tenta carregar um diretório de ambiente virtual inválido ou corrompido.
- Recrie o ambiente virtual rodando `uv sync`.
- Verifique se a configuração do servidor aponta diretamente para o interpretador correto em `.venv/bin/python`, se aplicável.

### `No module named fastmcp.__main__`
Use o módulo de linha de comando oficial do FastMCP ao executar via interpretador Python:
```bash
python -m fastmcp.cli run src/claude_code_mcp/server.py --transport stdio
```

### `claude not found in PATH`
Certifique-se de que o executável oficial do Claude Code está instalado e visível em sua variável `PATH`.
```bash
which claude
```
Se necessário, configure a variável de ambiente `CLAUDE_MCP_CLAUDE_PATH` com o caminho absoluto para o binário `claude`.

### `workspace_path is outside allowed roots`
Adicione o diretório onde você está trabalhando à lista de caminhos permitidos na variável `CLAUDE_MCP_ALLOWED_ROOTS`:
```json
"CLAUDE_MCP_ALLOWED_ROOTS": "[\"/caminho/para/workspace\", \"/outro/caminho\"]"
```

## 4) Fontes de Configuração

Opção A (Recomendada): Defina as variáveis de ambiente diretamente na configuração do servidor MCP no arquivo JSON do Trae (campo `env`).

Opção B: Utilize um arquivo `.env` local na pasta do servidor MCP:
```bash
cp .env.example .env
```

Ordem de precedência das configurações:
1. Variáveis de ambiente configuradas no bloco `env` do Trae.
2. Variáveis de ambiente definidas no arquivo `.env` do servidor.
3. Valores padrão embutidos no código.

## 5) Exemplos de Chamadas

### 5.1 Health Check (Verificação de Saúde)

Chamada da ferramenta `claude_health`:

```json
{
  "expected_version": "0.1.0"
}
```

### 5.2 Execução Síncrona (Sync Run)

Chamada da ferramenta `claude_run_task`:

```json
{
  "workspace_path": "/caminho/para/projeto",
  "prompt": "Adicionar docstrings para todas as funcoes no arquivo main.py",
  "capture_changes": true,
  "change_scope": "workspace",
  "options": {
    "sandbox": true,
    "dangerously_skip_permissions": false,
    "timeout_s": 300,
    "env": null,
    "extra_args": []
  }
}
```

### 5.3 Execução Assíncrona (Async Run)

Iniciar a tarefa (`claude_start_task`):

```json
{
  "workspace_path": "/caminho/para/projeto",
  "prompt": "Refatorar o projeto para usar tipagem estatica",
  "capture_changes": true,
  "change_scope": "workspace",
  "options": {
    "sandbox": true,
    "timeout_s": 600
  }
}
```

Consultar progresso da tarefa (`claude_poll_task`):

```json
{
  "run_id": "run-uuid-gerado-no-start"
}
```

Cancelar tarefa ativa (`claude_cancel_task`):

```json
{
  "run_id": "run-uuid-gerado-no-start",
  "force": false
}
```

### 5.4 Listar Execuções Recentes

Chamada da ferramenta `claude_list_runs`:

```json
{
  "limit": 10
}
```

### 5.5 Operações de Persistência

Inicializar o diretório de persistência (`claude_init_persistence`):

```json
{
  "force": false,
  "seed_templates": true
}
```

Ler um arquivo de persistência (`claude_read_persistence`):

```json
{
  "file": "memory",
  "offset": 0,
  "limit": null
}
```

Adicionar conteúdo a um arquivo de persistência (`claude_append_persistence`):

```json
{
  "file": "memory",
  "content": "A refatoracao do modulo de autenticacao foi concluida com sucesso na sessao anterior.",
  "section_header": "Takeaways do Projeto",
  "confirm": false
}
```

## Notas

### Seleção de Modelo

Este servidor MCP suporta a seleção do modelo por chamada (via campo `model` nos requests ou via parâmetro de linha de comando `--model`). Ao contrário do servidor MCP do Antigravity CLI (`agy`), que configura o modelo de forma interativa e persistente na CLI, o Claude Code MCP permite especificar aliases curtos (como `"sonnet"` ou `"opus"`) ou o nome completo do modelo diretamente no payload das requisições síncronas e assíncronas.

### Carregamento Automático de Contexto Persistente

Quando ativado, o servidor MCP localiza a pasta de persistência em `~/.open-cli-router/claude-code/` e injeta automaticamente trechos dos arquivos `AGENTS.md`, `PROJECTS.md` e `MEMORY.md` no prompt enviado ao processo do Claude Code CLI. Isso permite manter a identidade do agente, regras do projeto e históricos de aprendizado ativos de forma automática entre diferentes sessões de trabalho.
