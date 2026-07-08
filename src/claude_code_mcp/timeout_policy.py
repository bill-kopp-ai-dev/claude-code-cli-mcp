"""Módulo de Governança de Timeouts ciente da complexidade da tarefa.

Este arquivo decide dinamicamente a recomendação de tempo limite e determina se a operação
deve obrigatoriamente trafegar por um canal assíncrono para evitar falhas de rede no FastMCP.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from claude_code_mcp.models import ModelProfile, TaskClass

# Teto fixo para chamadas síncronas do FastMCP, acima do qual a comunicação é interrompida.
SYNC_CEILING_S: int = 600

# Teto absoluto suportado pelos validadores Pydantic do servidor.
ASYNC_CEILING_S: int = 3600


@dataclass(frozen=True)
class TimeoutRecommendation:
    """Contrato de retorno contendo a recomendação gerada pelo mecanismo de política."""
    timeout_s: int
    must_use_async: bool
    warning: Optional[str] = None


def compute_timeout(
    task_class: TaskClass,
    model_profile: ModelProfile,
    files_to_edit: int = 1,
    max_budget_usd: Optional[float] = None,
) -> TimeoutRecommendation:
    """Calcula a recomendação ideal de timeout e modalidade de chamada.
    
    Ajusta dinamicamente a duração limite com base na classe de complexidade da tarefa,
    na quantidade estimada de arquivos impactados, na capacidade do modelo e nas restrições
    de orçamento financeiro do chamador.
    
    Args:
        task_class: Categoria de complexidade da tarefa.
        model_profile: Perfil do modelo selecionado obtido do MODEL_REGISTRY.
        files_to_edit: Quantidade estimada de arquivos a serem criados ou editados.
        max_budget_usd: Limite de orçamento em USD estipulado para a execução.
        
    Returns:
        Um objeto TimeoutRecommendation contendo os parâmetros de execução.
    """
    # Mapeamento do tempo base recomendado em segundos por classe de tarefa
    base_timeouts: dict[TaskClass, int] = {
        TaskClass.TRIVIAL_EDIT: 180,                  # 3 minutos
        TaskClass.SMOKE_TEST: 120,                    # 2 minutos
        TaskClass.SINGLE_FEATURE: 300,                # 5 minutos
        TaskClass.DOCS_UPDATE: 240,                   # 4 minutos
        TaskClass.TEST_SUITE: 360,                    # 6 minutos
        TaskClass.REVIEW: 180,                        # 3 minutos
        TaskClass.MULTI_FILE_REFACTOR: 900,           # 15 minutos (requer assíncrono)
        TaskClass.ARCHITECTURE: 1800,                 # 30 minutos (requer assíncrono)
        TaskClass.MIGRATION: 1500,                    # 25 minutos (requer assíncrono)
        TaskClass.LONG_RUNNING_DATA_AGENT: 3600,      # 60 minutos (teto máximo)
    }
    
    timeout_s = base_timeouts.get(task_class, 600)
    
    # Penalização de timeout por quantidade de arquivos editados (fator volumétrico)
    if files_to_edit >= 50:
        timeout_s = max(timeout_s, 1800)  # Garante ao menos 30 minutos
    elif files_to_edit >= 20:
        timeout_s = max(timeout_s, 900)   # Garante ao menos 15 minutos
    elif files_to_edit >= 5:
        timeout_s = max(timeout_s, 600)   # Garante ao menos 10 minutos
        
    warning_msgs: list[str] = []
    
    # Validação de segurança: Modelos leves em tarefas massivas de escrita
    if not model_profile.multi_file_safe and files_to_edit > 5:
        warning_msgs.append(
            f"O modelo '{model_profile.display_name}' não é recomendado para tarefas "
            f"que alteram múltiplos arquivos ({files_to_edit} detectados). "
            f"Isso pode gerar alucinações de escrita ou interrupções de contexto. "
            f"Recomenda-se migrar para Sonnet 5, Fable 5 ou Opus 4.8."
        )
        
    # Análise de viabilidade econômica (Heurística de estouro de orçamento)
    if max_budget_usd is not None:
        # Estima o custo com base no tempo consumido relativo a 10 minutos de uso padrão
        estimated_cost = model_profile.typical_cost_per_run_usd * (timeout_s / 600.0)
        if estimated_cost > max_budget_usd * 0.8:
            warning_msgs.append(
                f"O custo estimado desta execução (${estimated_cost:.2f} USD) compromete mais de "
                f"80% do orçamento total estipulado de ${max_budget_usd:.2f} USD. "
                f"Considere reduzir o esforço ou adotar um modelo de tier mais econômico."
            )
            
    # Determinação da necessidade de migração para transporte assíncrono
    must_use_async = timeout_s > SYNC_CEILING_S
    
    # Garante que o timeout calculado respeite os limites absolutos de infraestrutura
    timeout_s = max(60, min(ASYNC_CEILING_S, timeout_s))
    
    warning = " | ".join(warning_msgs) if warning_msgs else None
    
    return TimeoutRecommendation(
        timeout_s=timeout_s,
        must_use_async=must_use_async,
        warning=warning
    )
