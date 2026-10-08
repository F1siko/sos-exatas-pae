"""SOS Exatas — Sistema Integrado PAE (Conectado ao Supabase & Blindado).

Execução:
    streamlit run app_sos_exatas.py

Variáveis de ambiente OBRIGATÓRIAS (o app NÃO inicia se faltar qualquer uma):
    SUPABASE_URL        — URL do projeto Supabase (SEM /rest/v1/ no final)
    SUPABASE_KEY        — Chave `service_role` rotacionada do Supabase (NUNCA commitar)
    SOS_SENHA_INICIAL    — Senha inicial dos usuários padrão (troca obrigatória
                        no primeiro acesso). Deve ter ≥12 caracteres.

Variáveis opcionais:
    SOS_LOG_LEVEL       — DEBUG | INFO | WARNING | ERROR (padrão: INFO)
    SOS_SESSAO_MIN       — minutos de inatividade até expirar a sessão (padrão: 60)
"""
from __future__ import annotations

import hashlib
import hmac
import io
import json
import logging
import os
import re
import secrets
import sys
import unicodedata
import uuid
from dataclasses import dataclass, field, asdict
from datetime import date, datetime, timedelta, timezone
from enum import Enum
from html import escape as esc
from pathlib import Path
from typing import Optional
from xml.sax.saxutils import escape as xml_esc

import pandas as pd
import plotly.express as px
import streamlit as st
from supabase import create_client, Client

# ---------------------------------------------------------------------------
# Configuração de logging estruturado (JSON)
# ---------------------------------------------------------------------------
_LOG_LEVEL = os.environ.get("SOS_LOG_LEVEL", "INFO").upper()

class _JsonFormatter(logging.Formatter):
    def format(self, record):
        payload = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "nivel": record.levelname,
            "modulo": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


_handler = logging.StreamHandler(sys.stderr)
_handler.setFormatter(_JsonFormatter())
logging.basicConfig(level=getattr(logging, _LOG_LEVEL, logging.INFO), handlers=[_handler])
log = logging.getLogger("sos_exatas")

logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)


# ---------------------------------------------------------------------------
# Helpers de configuração — fail-closed
# ---------------------------------------------------------------------------
def _env_obrigatoria(nome: str, minimo: int = 1) -> str:
    valor = os.environ.get(nome, "").strip()
    if not valor:
        raise RuntimeError(
            f"Variável de ambiente obrigatória ausente: {nome}. "
            f"O app não inicia sem ela (fail-closed)."
        )
    if len(valor) < minimo:
        raise RuntimeError(f"{nome} tem menos de {minimo} caracteres.")
    return valor


def _env_opcional(nome: str, padrao: str = "") -> str:
    return os.environ.get(nome, padrao).strip()


SUPABASE_URL = _env_obrigatoria("SUPABASE_URL")
SUPABASE_KEY = _env_obrigatoria("SUPABASE_KEY", minimo=40)
TEMPO_SESSAO_MIN = int(_env_opcional("SOS_SESSAO_MIN", "60"))


def _senha_inicial() -> str:
    return _env_obrigatoria("SOS_SENHA_INICIAL", minimo=12)


supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
log.info("Supabase client criado com sucesso")

st.set_page_config(
    page_title="SOS Exatas — Sistema Integrado PAE",
    page_icon="🧭",
    layout="wide",
    initial_sidebar_state="expanded"
)


def _versao_st() -> tuple:
    try:
        return tuple(int(x) for x in st.__version__.split(".")[:2])
    except Exception:
        return (1, 0)


W = {"width": "stretch"} if _versao_st() >= (1, 50) else {"use_container_width": True}

st.markdown("""
<style>
    .stButton>button { border-radius: 8px; font-weight: 600; }
    div[data-testid="stMetricValue"] { color: #1E3A8A; }
    .titulo-pp { background: linear-gradient(90deg, #1E3A8A 0%, #2748A0 100%); padding: 20px;
        border-radius: 10px; margin-bottom: 15px; box-shadow: 0 4px 12px rgba(30,58,138,.15); }
    .titulo-pp h2 { color: #FFD700; margin: 0; font-size: 1.6rem; }
    .titulo-pp p { color: #E8EEFB; margin: 4px 0 0 0; font-size: .95rem; }
    .timbrado-institucional { background:#fff; border:1px solid #cbd5e1; border-top:5px solid #1E3A8A;
        border-bottom:4px solid #D97706; padding:28px; border-radius:10px; margin-bottom:24px; color:#0f172a; }
    .timbrado-header { display:flex; justify-content:space-between; align-items:center;
        border-bottom:2px solid #e2e8f0; padding-bottom:16px; margin-bottom:18px; }
    .card-ciclo-aberto { background:#fffbeb; border:1px solid #fde68a; border-left:5px solid #D97706;
        padding:14px; border-radius:8px; margin-bottom:12px; color:#0f172a; }
    .card-diario-pauta { background:#eff6ff; border:1px solid #bfdbfe; border-left:5px solid #1E3A8A;
        padding:14px; border-radius:8px; margin-bottom:12px; color:#0f172a; }
    .card-renovacao { background:#fff1f2; border:2px solid #e11d48; padding:16px; border-radius:8px;
        color:#9f1239; margin-bottom:15px; }
    .card-comunicado { background:#f8fafc; border:1px solid #e2e8f0; border-left:5px solid #2563eb;
        padding:12px 16px; border-radius:6px; margin-bottom:10px; color:#0f172a; }
</style>
""", unsafe_allow_html=True)

# ==============================================================================
# DIRETÓRIOS E CONSTANTES
# ==============================================================================
PASTA_FICHAS = "fichas_professores"
PASTA_ENVIOS_ALUNOS = "envios_alunos"
PASTA_DOCS_PROFESSORES = "docs_professores"
NOTIF_DIR = Path("dados/notificacoes")
NOTAS_DIR = Path("dados/notas")

for _p in [PASTA_FICHAS, PASTA_ENVIOS_ALUNOS, PASTA_DOCS_PROFESSORES,
           NOTIF_DIR, NOTAS_DIR]:
    os.makedirs(_p, exist_ok=True)

MAX_FALHAS = 5
BLOQUEIO_MIN = 5

MODALIDADES_VALIDAS = [
    "Mentoria Acadêmica Presencial",
    "Mentoria Acadêmica Virtual",
    "Banca de Estudos"
]


# ==============================================================================
# ENUMS E CONSTANTES PEDAGÓGICAS
# ==============================================================================
class Disciplina(str, Enum):
    MATEMATICA = "Matemática"
    FISICA = "Física"
    QUIMICA = "Química"
    BIOLOGIA = "Biologia"
    PORTUGUES = "Português"
    REDACAO = "Redação"
    CIENCIAS = "Ciências"


class Etapa(str, Enum):
    FUNDAMENTAL = "Ensino Fundamental"
    MEDIO = "Ensino Médio"


class UnidadeTematica(str, Enum):
    NUMEROS = "Números"
    ALGEBRA = "Álgebra"
    GEOMETRIA = "Geometria"
    GRANDEZAS = "Grandezas e Medidas"
    PROBABILIDADE = "Probabilidade e Estatística"
    MATERIA_ENERGIA = "Matéria e Energia"
    VIDA_EVOLUCAO = "Vida e Evolução"
    TERRA_UNIVERSO = "Terra e Universo"


@dataclass(frozen=True)
class TopicoSOS:
    codigo: str
    nome: str
    area: str


TOPICOS_SOS: dict[str, TopicoSOS] = {
    "M1": TopicoSOS("M1", "Aritmética e Números Reais", "Matemática"),
    "M2": TopicoSOS("M2", "Geometria Plana e Espacial", "Matemática"),
    "M3": TopicoSOS("M3", "Álgebra e Equações", "Matemática"),
    "M4": TopicoSOS("M4", "Funções e Gráficos", "Matemática"),
    "M5": TopicoSOS("M5", "Trigonometria", "Matemática"),
    "M6": TopicoSOS("M6", "Estatística e Probabilidade", "Matemática"),
    "M7": TopicoSOS("M7", "Exponenciais e Logaritmos", "Matemática"),
    "F1": TopicoSOS("F1", "Cinemática", "Física"),
    "F2": TopicoSOS("F2", "Dinâmica Newtoniana", "Física"),
    "F3": TopicoSOS("F3", "Trabalho, Energia e Potência", "Física"),
    "F4": TopicoSOS("F4", "Impulso e Quantidade de Movimento", "Física"),
    "F5": TopicoSOS("F5", "Hidrostática", "Física"),
    "F6": TopicoSOS("F6", "Termologia e Termodinâmica", "Física"),
    "F7": TopicoSOS("F7", "Óptica Geométrica", "Física"),
    "F8": TopicoSOS("F8", "Eletricidade e Magnetismo", "Física"),
    "Q1": TopicoSOS("Q1", "Estrutura Atômica e Tabela Periódica", "Química"),
    "Q2": TopicoSOS("Q2", "Ligações e Reações Químicas", "Química"),
    "Q3": TopicoSOS("Q3", "Estequiometria", "Química"),
    "Q4": TopicoSOS("Q4", "Soluções e Concentração", "Química"),
    "B1": TopicoSOS("B1", "Citologia", "Biologia"),
    "B2": TopicoSOS("B2", "Genética", "Biologia"),
    "B3": TopicoSOS("B3", "Ecologia", "Biologia"),
}

TAXONOMIA_ERRO_PEDAGOGICO: list[str] = [
    "Interpretação", "Modelagem", "Cálculo", "Álgebra básica",
    "Distração", "Lacuna conceitual", "Memorização frágil",
    "Falta de pré-requisito", "Estratégia inadequada", "Gestão de tempo"
]


# ==============================================================================
# UTILITÁRIOS DE SEGURANÇA
# ==============================================================================
def nome_seguro(nome: str) -> str:
    return re.sub(r"[^\w.\-]", "_", Path(nome).name)[:120] or "arquivo"


def _caminho_storage_seguro(disciplina: str, serie: str, nome_arquivo: str) -> str:
    def limpar(texto):
        nfkd = unicodedata.normalize('NFKD', str(texto))
        return "".join([c for c in nfkd if not unicodedata.combining(c)]).replace(" ", "_")
    
    disc_clean = limpar(disciplina)
    serie_clean = limpar(serie)
    nome_unico = f"{uuid.uuid4().hex[:8]}_{nome_seguro(nome_arquivo)}"
    return f"{disc_clean}/{serie_clean}/{nome_unico}"


def gerar_hash(senha: str, salt: Optional[str] = None) -> str:
    salt = salt or secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", senha.encode("utf-8"), bytes.fromhex(salt), 600_000)
    return f"pbkdf2${salt}${dk.hex()}"


def verificar_senha(senha: str, armazenado: str) -> bool:
    if not armazenado or not armazenado.startswith("pbkdf2$"):
        return False
    try:
        _, salt, _ = armazenado.split("$", 2)
    except ValueError:
        return False
    return hmac.compare_digest(gerar_hash(senha, salt), armazenado)


def senha_valida(senha: str) -> Optional[str]:
    if len(senha) < 12:
        return "A senha deve ter ao menos 12 caracteres."
    if not re.search(r"\d", senha) or not re.search(r"[A-Za-z]", senha):
        return "A senha deve conter letras e números."
    return None


class Perfil(str, Enum):
    FAMILIA = "familia"
    ALUNO = "aluno"
    PROFESSOR = "professor"
    COORDENADOR = "coordenador"
    ADMIN = "admin"


_BASE_PROF = {"visualizar", "criar", "editar", "excluir", "anexar", "exportar", "comentar",
              "notificar", "lancar_atendimento", "gerenciar_agenda", "gerenciar_contratos"}
PERMISSOES = {
    Perfil.FAMILIA: {"visualizar", "exportar", "comentar"},
    Perfil.ALUNO: {"visualizar", "enviar_material", "responder_diario"},
    Perfil.PROFESSOR: _BASE_PROF,
    Perfil.COORDENADOR: (_BASE_PROF - {"excluir"}) | {"gerenciar_matriz", "matricular",
                                                   "cadastrar_professor", "gerenciar_usuarios", "gerenciar_agenda", "gerenciar_contratos"},
    Perfil.ADMIN: _BASE_PROF | {"gerenciar_matriz", "matricular", "cadastrar_professor",
                                "gerenciar_usuarios", "excluir_aluno", "auditoria", "editar_diretorio", "gerenciar_agenda", "gerenciar_contratos"},
}


def pode(perfil, acao: str) -> bool:
    if isinstance(perfil, str):
        try:
            perfil = Perfil(perfil.lower())
        except ValueError:
            return False
    return acao in PERMISSOES.get(perfil, set())


def exigir(perfil, acao: str) -> None:
    if not pode(perfil, acao):
        st.error("🔒 Você não tem permissão para acessar esta área.")
        st.stop()


# ==============================================================================
# HELPERS DE UI E ARQUIVOS
# ==============================================================================
def card(classe_css: str, html_conteudo: str) -> None:
    st.markdown(f'<div class="{classe_css}">{html_conteudo}</div>', unsafe_allow_html=True)


def salvar_upload(upload, pasta: str | Path, prefixo: str) -> str:
    pasta = Path(pasta)
    pasta.mkdir(parents=True, exist_ok=True)
    ext = Path(upload.name).suffix.lower()
    stem = re.sub(r"[^\w\-]", "_", Path(upload.name).stem)[:60] or "arquivo"
    nome_final = f"{prefixo}_{uuid.uuid4().hex[:8]}_{stem}{ext}"
    (pasta / nome_final).write_bytes(upload.getbuffer())
    return nome_final


def botao_download(label: str, pasta: str | Path, nome_arquivo: str, key: str) -> None:
    caminho = Path(pasta) / nome_arquivo
    if not caminho.exists():
        st.warning(f"⚠️ Arquivo indisponível: {nome_arquivo}")
        return
    st.download_button(
        label,
        data=caminho.read_bytes(),
        file_name=nome_arquivo,
        key=key,
        **W,
    )


# ==============================================================================
# HELPERS DE CONFIGURAÇÃO / PEDAGÓGICOS
# ==============================================================================
def periodos_disponiveis(aluno: Optional[dict] = None) -> list[str]:
    base = list(DB()["config"].get("periodos", []))
    if aluno:
        for p in aluno.get("planejamentos_pedagogicos", []):
            per = p.get("periodo")
            if per and per not in base:
                base.append(per)
    return base


def disciplinas_disponiveis() -> list[str]:
    return list(DB()["config"].get("disciplinas", []))


def parecer_automatico(aluno: dict) -> str:
    d = aluno.get("dados", {})
    op = aluno.get("operacao", {})
    ats = aluno.get("atendimentos_processo", [])
    ciclos = aluno.get("ciclos_intervencao", [])
    fechados = sum(1 for c in ciclos if "Fechado" in str(c.get("status", "")))
    abertos = sum(1 for c in ciclos if "Fechado" not in str(c.get("status", "")))

    if ats:
        ganhos = [float(a.get("ganho_ipsativo", 0) or 0) for a in ats]
        media_ganho = sum(ganhos) / len(ganhos)
    else:
        media_ganho = 0.0

    if media_ganho >= 15:
        recomendacao = "Recomenda-se manter o ritmo atual e reforçar a revisão espaçada D+7/D+30."
    elif media_ganho > 0:
        recomendacao = "Recomenda-se consolidar as habilidades-foco com listas estruturantes adicionais."
    else:
        recomendacao = "Recomenda-se revisar pré-requisitos e redesenhar a abordagem didática dos blocos com maior dificuldade."

    return (
        f"{d.get('nome', 'O(A) estudante')} cumpriu "
        f"{op.get('horas_realizadas', 0):.1f}h de {op.get('horas_contratadas', 0):.1f}h contratadas, "
        f"com {op.get('presencas', 0)} presença(s) e {op.get('faltas', 0)} falta(s). "
        f"Foram registrados {len(ats)} atendimento(s), com ganho ipsativo médio de "
        f"{media_ganho:+.1f} p.p. por sessão. Ciclos fechados com sucesso: {fechados}; "
        f"em andamento: {abertos}. {recomendacao}"
    )


def renderizar_widget_notificacoes(aluno_id: str) -> None:
    notifs = carregar_notificacoes(aluno_id)
    if not notifs:
        return

    nao_lidas = sum(1 for n in notifs if not n.get("lida"))
    with st.popover(f"🔔 Notificações ({nao_lidas} não lida(s))"):
        c1, c2 = st.columns([3, 1])
        c1.markdown(f"**{len(notifs)} notificação(ões) no total**")
        if nao_lidas and c2.button("Marcar todas como lidas", key=f"mtl_{aluno_id}"):
            marcar_todas_lidas(aluno_id)
            st.rerun()

        icones = {"sucesso": "✅", "info": "ℹ️", "alerta": "⚠️", "erro": "❌"}
        for i, n in enumerate(notifs[:20]):
            icone = icones.get(n.get("tipo"), "🔔")
            estado = "" if n.get("lida") else " **(nova)**"
            with st.container(border=True):
                st.markdown(f"{icone} **{esc(n.get('titulo', ''))}**{estado}")
                st.caption(f"{str(n.get('criada_em', ''))[:16]} • origem: {esc(n.get('origem', 'sistema'))}")
                st.write(esc(str(n.get("mensagem", ""))))
                if not n.get("lida") and st.button("Marcar como lida", key=f"ml_{aluno_id}_{i}"):
                    marcar_como_lida(aluno_id, i)
                    st.rerun()


# ==============================================================================
# BANCO DE DADOS (SUPABASE CLOUD + VERSIONAMENTO OTIMISTA)
# ==============================================================================
_VERSAO_ESTADO = "versao"


def novo_id(prefixo: str) -> str:
    return f"{prefixo}-{uuid.uuid4().hex[:6].upper()}"


def proximo_id_aluno(alunos: dict) -> str:
    nums = [int(k.split("-")[1]) for k in alunos if re.fullmatch(r"ALU-\d+", k)]
    return f"ALU-{(max(nums) + 1) if nums else 101}"


HABILIDADES_PADRAO = [
    {"codigo": "EF06MA01", "descricao": "Comparar, ordenar, ler e escrever números naturais e racionais.", "etapa": "6º ano", "disciplina": "Matemática", "unidade": "Números", "topicos_sos": ["M1"], "prerequisitos": []},
    {"codigo": "EF07MA13", "descricao": "Diferenciar variável e incógnita.", "etapa": "7º ano", "disciplina": "Matemática", "unidade": "Álgebra", "topicos_sos": ["M3"], "prerequisitos": []},
    {"codigo": "EF07MA18", "descricao": "Resolver equações de 1º grau.", "etapa": "7º ano", "disciplina": "Matemática", "unidade": "Álgebra", "topicos_sos": ["M3"], "prerequisitos": ["EF07MA13"]},
    {"codigo": "EF09MA06", "descricao": "Compreender funções de 1º e 2º grau.", "etapa": "9º ano", "disciplina": "Matemática", "unidade": "Álgebra", "topicos_sos": ["M3", "M4"], "prerequisitos": []},
    {"codigo": "EF09MA13", "descricao": "Aplicar Teorema de Pitágoras.", "etapa": "9º ano", "disciplina": "Matemática", "unidade": "Geometria", "topicos_sos": ["M2"], "prerequisitos": []},
    {"codigo": "EF07CI01", "descricao": "Compreender máquinas simples.", "etapa": "7º ano", "disciplina": "Ciências", "unidade": "Matéria e Energia", "topicos_sos": ["F3"], "prerequisitos": []},
    {"codigo": "EM13MAT101", "descricao": "Interpretar criticamente variação de grandezas em gráficos.", "etapa": "1ª série EM", "disciplina": "Matemática", "unidade": "Álgebra", "topicos_sos": ["M3"], "prerequisitos": []},
    {"codigo": "EM13MAT304", "descricao": "Resolver problemas com função exponencial.", "etapa": "1ª série EM", "disciplina": "Matemática", "unidade": "Álgebra", "topicos_sos": ["M7"], "prerequisitos": []},
    {"codigo": "EM13CNT101", "descricao": "Analisar transformações e conservações em sistemas mecânicos.", "etapa": "1ª série EM", "disciplina": "Física", "unidade": "Matéria e Energia", "topicos_sos": ["F1", "F3"], "prerequisitos": ["EF07CI01"]},
    {"codigo": "EM13CNT102", "descricao": "Realizar previsões sobre sistemas térmicos e elétricos.", "etapa": "2ª série EM", "disciplina": "Física", "unidade": "Matéria e Energia", "topicos_sos": ["F6", "F8"], "prerequisitos": []},
    {"codigo": "EM13CNT104", "descricao": "Avaliar riscos e benefícios de materiais e reações.", "etapa": "1ª série EM", "disciplina": "Química", "unidade": "Matéria e Energia", "topicos_sos": ["Q1", "Q2"], "prerequisitos": []},
]


def _gerar_alunos_base_sos() -> dict:
    presenciais = [
        "Amanda Helena Silva Freire", "Beatriz Lima Figueiredo de Sá",
        "Caio Tolentino De Lira Alves Paz", "Daniel Brandt de Sampaio",
        "Davi Batista Ferreira da Silva Mousinho", "Gabriel Rodrigues Lacerda",
        "Helena Arruda Melo da Costa e Silva", "Isaac Menezes de Araújo",
        "João Gabriel Bispo Oliveira", "Larissa Maria Samico Cunha",
        "Laura Pinto Gusmão Paes", "Lucas Gabriel Sampaio Interaminense de Oliveira",
        "Lucas Peralta Gois", "Luiza Lessa Rocha", "Manoel Albuquerque",
        "Marco Antonio Gonçalves Pereira", "Maria da Conceição Pereira de Freitas",
        "Maria Luisa da Silva Lima", "Maria Luiza Araujo", "Maria Victoria Martins de Melo",
        "Marianna Carreras de Carvalho", "Marina da Franca Bandeira Ferreira Santos",
        "Matheus Felipe Alves da Silva", "Romero Alencar de Mendonça Canuto Filho",
        "Sara Lessa Rocha", "Sofia Gomes Silva", "Vinícius Macedo Quirino da Silva",
        "Yasmin Silva Lima", "Gabriela Bezerra Porto Carreiro", "Marília Diniz Manguinho"
    ]
    virtuais = [
        "Maria Júlia da Silva Lima", "Ryan Souza Duarte Teixeira",
        "Carolina Cavalcanti Santos", "Rodrigo Menezes Breckenfeld",
        "Theo Manasses Xavier Rodrigues", "José Guilherme Carneiro do Nascimento",
        "Isabela Evelly Nabu Santiago da Silva"
    ]
    banca = [
        "Caio Tolentino De Lira Alves Paz", "Helena Arruda Melo da Costa e Silva",
        "Gabriel Rodrigues Lacerda", "Marina da Franca Bandeira Ferreira Santos",
        "Lukas Dekian Barbosa Silva dos Santos", "Guilherme de Moura Falcão",
        "Gabriel de Moura Falcão", "Raul Alves de Azevedo", "Davi Suassuna West",
        "Beatriz Pires Campaner", "Elis Batista Jansen de Sá Cruz",
        "Maria Eduarda da Silva Gomes", "Heloísa Lisboa Kyrillos",
        "Ester Ferreira Ribeiro Costa Vanderlei", "Beatriz Nunes da Costa Simões",
        "Theo Manasses Xavier Rodrigues", "Vittor Madeira Costa de Amorim",
        "Eduarda Meireles Rodrigues", "Max André Albuquerque Cavalcante",
        "Laura Limeira de Moura Ramos", "Miguel de Lima Oliveira",
        "Vinicius da Franca Bandeira Ferreira Santos", "Ana Miranda de Alcântara Leite",
        "Marília Diniz Manguinho"
    ]

    todos_nomes = sorted(list(set(presenciais + virtuais + banca)))
    alunos_db = {}
    for idx, nome in enumerate(todos_nomes, start=101):
        aid = f"ALU-{idx}"
        mods = []
        if nome in presenciais:
            mods.append("Mentoria Acadêmica Presencial")
        if nome in virtuais:
            mods.append("Mentoria Acadêmica Virtual")
        if nome in banca:
            mods.append("Banca de Estudos")

        alunos_db[aid] = {
            "dados": {
                "id": aid,
                "nome": nome,
                "modalidade": mods[0] if mods else "Mentoria Acadêmica Presencial",
                "modalidades": mods,
                "escola": "Colégio de Aplicação / Geral",
                "serie": "3º Ano EM / Pré-Vestibular",
                "objetivo_estudante": "Medicina (SSA/UPE e ENEM)",
                "contato": "(81) 98888-0000",
                "responsaveis": f"Responsável por {nome}",
                "data_matricula": "2026-02-01",
                "foto_path": None,
                "banca_foco": "SSA/UPE (Medicina)"
            },
            "operacao": {
                "horas_contratadas": 40.0,
                "horas_realizadas": 12.0,
                "valor_hora_contrato": 130.0,
                "presencas": 8,
                "faltas": 0
            },
            "contratos": [
                {
                    "id": f"CONT-{idx}",
                    "numero_contrato": f"SOS-2026-{idx}",
                    "data_inicio": "2026-02-01",
                    "data_fim": "2026-12-15",
                    "status": "Vigente",
                    "valor_total": 5200.0,
                    "forma_pagamento": "Boleto / Pix Mensal",
                    "observacoes": "Contrato padrão PAE anual."
                }
            ],
            "parecer_coordenacao": "",
            "planejamentos_pedagogicos": [],
            "fichas_disponibilizadas": [],
            "materiais_enviados_aluno": [],
            "atendimentos_processo": [],
            "ciclos_intervencao": [],
            "autoavaliacoes_estudante": [],
            "revisoes_agendadas": []
        }
    return alunos_db


def _template_banco() -> dict:
    return {
        _VERSAO_ESTADO: 0,
        "usuarios": {},
        "config": {
            "periodos": ["2026 - 1º Bimestre", "2026 - 2º Bimestre", "2026 - 3º Bimestre",
                         "2026 - 4º Bimestre", "Outubro/2026"],
            "disciplinas": ["Física", "Matemática", "Química", "Biologia", "Ciências", "Redação"],
        },
        "bloqueios": {},
        "auditoria": [],
        "agenda": [
            {
                "id": "AG-001",
                "titulo": "Mentoria Coletiva de Física - Termodinâmica",
                "data": "2026-10-10",
                "horario": "14:00",
                "local_ou_link": "Sala Presencial 01 / Sede Graças",
                "responsavel": "Prof. Heitor Albuquerque",
                "tipo": "Aula Coletiva",
                "participantes": "Turma 3º Ano EM"
            }
        ],
        "professores": [
            {"id": "PROF-01", "nome": "Prof. Heitor Albuquerque", "disciplina": "Física", "valor_hora": 90.0, "contato": "(81) 99988-7766"},
            {"id": "PROF-02", "nome": "Prof. Ésio Tavares", "disciplina": "Química", "valor_hora": 90.0, "contato": "(81) 98877-5544"},
            {"id": "PROF-03", "nome": "Prof. Lucas Mendes", "disciplina": "Matemática", "valor_hora": 75.0, "contato": "(81) 97766-3322"},
        ],
        "habilidades_cadastradas": HABILIDADES_PADRAO,
        "mensagens_familias": [],
        "docs_professores": [],
        "alunos": _gerar_alunos_base_sos(),
    }


def criar_banco_padrao() -> dict:
    dados = _template_banco()
    h = gerar_hash(_senha_inicial())

    def usr(nome, perfil, vinc=None):
        return {"nome": nome, "hash_senha": h, "perfil": perfil, "aluno_vinculado": vinc, "trocar_senha": True}

    dados["usuarios"] = {
        "admin": usr("Administrador SOS Exatas", "admin"),
        "coordenacao": usr("Coordenação SOS Exatas", "coordenador"),
        "professor": usr("Prof. Heitor Albuquerque", "professor"),
        "aluno": usr("Caio Tolentino (Aluno)", "aluno", "ALU-106"),
        "familia": usr("Família Tolentino (Responsáveis)", "familia", "ALU-106"),
    }
    dados["mensagens_familias"] = [
        {"id": "MSG-000001", "data": "2026-09-28", "aluno_id": "ALU-106", "remetente": "Responsável por Caio Tolentino",
         "mensagem": "Gostaria de alinhar o cronograma da mentoria com os simulados da escola.",
         "respondida": False, "respostas": []}
    ]
    return dados


def sanitizar_banco(dados: dict) -> dict:
    padrao = _template_banco()
    for k in ["usuarios", "professores", "alunos", "habilidades_cadastradas", "mensagens_familias",
              "docs_professores", "config", "bloqueios", "auditoria", "agenda"]:
        dados.setdefault(k, padrao[k])
    dados.setdefault(_VERSAO_ESTADO, 0)
    dados["config"].setdefault("periodos", padrao["config"]["periodos"])
    dados["config"].setdefault("disciplinas", padrao["config"]["disciplinas"])

    for p in dados["professores"]:
        p.setdefault("contato", "(81) 99999-0000")
    for m in dados["mensagens_familias"]:
        m.setdefault("id", novo_id("MSG"))
        m.setdefault("respostas", [])
        m.setdefault("respondida", False)

    if not dados.get("alunos") or len(dados["alunos"]) < 10:
        dados["alunos"] = _gerar_alunos_base_sos()

    for al in dados["alunos"].values():
        d = al.setdefault("dados", {})
        if "modalidades" not in d:
            d["modalidades"] = [d.get("modalidade", "Mentoria Acadêmica Presencial")]

        al.setdefault("contratos", [])
        for k in ["planejamentos_pedagogicos", "fichas_disponibilizadas", "materiais_enviados_aluno",
                  "ciclos_intervencao", "autoavaliacoes_estudante", "revisoes_agendadas", "atendimentos_processo"]:
            al.setdefault(k, [])
        al.setdefault("parecer_coordenacao", "")
    return dados


def ciclos_vencidos(aluno: dict) -> list:
    hoje = str(date.today())
    return [c for c in aluno.get("ciclos_intervencao", [])
            if "Fechado" not in c["status"] and c.get("data_limite", "9999") < hoje]


class ConflitoDeVersao(RuntimeError):
    """Levantada quando outra sessão gravou dados no Supabase concorrentemente."""


def salvar_banco(dados: Optional[dict] = None) -> None:
    dados = dados if dados is not None else st.session_state.db
    versao_local = dados.get(_VERSAO_ESTADO, 0)
    nova_versao = versao_local + 1
    dados[_VERSAO_ESTADO] = nova_versao

    payload = {
        "id": "estado_global",
        "dados": dados,
        "atualizado_em": datetime.now(timezone.utc).isoformat(),
    }

    try:
        if versao_local == 0:
            resp = (
                supabase.table("sistema_estado")
                .update(payload)
                .eq("id", "estado_global")
                .execute()
            )
        else:
            resp = (
                supabase.table("sistema_estado")
                .update(payload)
                .eq("id", "estado_global")
                .eq("dados->>versao", str(versao_local))
                .execute()
            )
        if not resp.data:
            dados[_VERSAO_ESTADO] = versao_local
            raise ConflitoDeVersao("Outra sessão atualizou os dados. Suas últimas alterações foram descartadas — refaça-as.")
        _carregar_banco_remoto.clear()
        log.info(f"Estado salvo versao={nova_versao}")
    except ConflitoDeVersao:
        raise
    except Exception as e:
        dados[_VERSAO_ESTADO] = versao_local
        log.error(f"Falha ao salvar no Supabase: {type(e).__name__}: {e}")
        st.error("Não foi possível salvar os dados. Verifique sua conexão e tente novamente.")
        raise


@st.cache_data(ttl=15, show_spinner=False)
def _carregar_banco_remoto() -> Optional[dict]:
    try:
        resp = (
            supabase.table("sistema_estado")
            .select("dados")
            .eq("id", "estado_global")
            .execute()
        )
        if resp.data:
            return resp.data[0]["dados"]
    except Exception as e:
        log.warning(f"Falha ao carregar do Supabase: {type(e).__name__}: {e}")
    return None


def carregar_banco(forcar: bool = False) -> dict:
    if forcar:
        _carregar_banco_remoto.clear()

    remoto = _carregar_banco_remoto()
    if remoto:
        dados = sanitizar_banco(remoto)
        if _VERSAO_ESTADO not in remoto:
            log.info("Migrando registro sem campo versao remoto")
            try:
                supabase.table("sistema_estado").update({
                    "id": "estado_global",
                    "dados": dados,
                    "atualizado_em": datetime.now(timezone.utc).isoformat(),
                }).eq("id", "estado_global").execute()
                _carregar_banco_remoto.clear()
            except Exception as e:
                log.error(f"Falha na migração de versão: {type(e).__name__}: {e}")
        return dados

    dados = criar_banco_padrao()
    try:
        supabase.table("sistema_estado").insert({
            "id": "estado_global",
            "dados": dados,
            "atualizado_em": datetime.now(timezone.utc).isoformat(),
        }).execute()
        log.info("Bootstrap concluído no Supabase")
    except Exception as e:
        log.error(f"Bootstrap falhou ao gravar no Supabase: {e}")
    return dados


def _flash(nivel: str, msg: str) -> None:
    st.session_state.setdefault("_flash", []).append((nivel, msg))


def _render_flash() -> None:
    for nivel, msg in st.session_state.pop("_flash", []):
        getattr(st, nivel)(msg)


def _salvar_e_recarregar(sucesso: str = "Salvo com sucesso.") -> None:
    try:
        salvar_banco()
    except ConflitoDeVersao as e:
        _flash("warning", str(e))
        _carregar_banco_remoto.clear()
        st.session_state.db = carregar_banco(forcar=True)
        st.rerun()
        return
    _flash("success", sucesso)
    st.rerun()


def DB() -> dict:
    return st.session_state.db


def auditar(acao: str, detalhe: str = "") -> None:
    log_reg = DB().setdefault("auditoria", [])
    log_reg.append({
        "quando": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "usuario": st.session_state.get("usuario_key", "-"),
        "acao": acao,
        "detalhe": detalhe,
    })
    del log_reg[:-1000]


# ==============================================================================
# NOTIFICAÇÕES & NOTAS
# ==============================================================================
@dataclass
class Notificacao:
    aluno_id: str
    titulo: str
    mensagem: str
    tipo: str = "info"
    lida: bool = False
    criada_em: str = field(default_factory=lambda: datetime.now().isoformat())
    origem: str = "sistema"


def _ler_json(arq: Path) -> list:
    if not arq.exists():
        return []
    try:
        return json.loads(arq.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []


def _gravar_json(arq: Path, lista: list) -> None:
    tmp = arq.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(lista, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, arq)


def carregar_notificacoes(aluno_id: str) -> list:
    return _ler_json(NOTIF_DIR / f"{aluno_id}.json")


def _salvar_notif(aluno_id: str, lista: list) -> None:
    _gravar_json(NOTIF_DIR / f"{aluno_id}.json", lista)


def criar_notificacao(notif: Notificacao) -> None:
    lista = carregar_notificacoes(notif.aluno_id)
    lista.insert(0, asdict(notif))
    _salvar_notif(notif.aluno_id, lista[:200])


def marcar_como_lida(aluno_id: str, indice: int) -> None:
    lista = carregar_notificacoes(aluno_id)
    if 0 <= indice < len(lista):
        lista[indice]["lida"] = True
        _salvar_notif(aluno_id, lista)


def marcar_todas_lidas(aluno_id: str) -> None:
    lista = carregar_notificacoes(aluno_id)
    for n in lista:
        n["lida"] = True
    _salvar_notif(aluno_id, lista)


def limpar_notificacoes(aluno_id: str) -> None:
    _salvar_notif(aluno_id, [])


def contar_nao_lidas(aluno_id: str) -> int:
    return sum(1 for n in carregar_notificacoes(aluno_id) if not n.get("lida"))


def notificar(aluno_id: str, titulo: str, msg: str, tipo="info", origem="sistema") -> None:
    criar_notificacao(Notificacao(aluno_id, titulo, msg, tipo, origem=origem))


def salvar_nota(aluno_id: str, disciplina: str, periodo: str, avaliacao: str, nota: float, peso: float = 1.0) -> None:
    lista = _ler_json(NOTAS_DIR / f"{aluno_id}.json")
    lista.append({"disciplina": disciplina, "periodo": periodo, "avaliacao": avaliacao,
                  "nota": float(nota), "peso": float(peso), "lancada_em": datetime.now().isoformat()})
    _gravar_json(NOTAS_DIR / f"{aluno_id}.json", lista)
    notificar(aluno_id, "✅ Nota lançada", f"Nota de {disciplina}: {nota:.1f}", "sucesso")


def media_por_disciplina(aluno_id: str) -> dict:
    agregado: dict = {}
    for n in _ler_json(NOTAS_DIR / f"{aluno_id}.json"):
        agregado.setdefault(n["disciplina"], []).append((n["nota"], n["peso"]))
    return {d: sum(n * p for n, p in pares) / (sum(p for _, p in pares) or 1) for d, pares in agregado.items()}


# ==============================================================================
# EXPORTAÇÃO
# ==============================================================================
def exportar_excel(planejamentos: list) -> bytes | None:
    try:
        from openpyxl.styles import Font, PatternFill, Alignment
    except ImportError:
        return None
    linhas = [{
        "Período": p.get("periodo", ""), "Disciplina": p.get("disciplina", ""),
        "Professor(a)": p.get("professor_responsavel", ""), "Objetivo Geral": p.get("objetivo_geral", ""),
        "Conteúdos": " | ".join(p.get("conteudos", [])), "Metodologia": p.get("metodologia", ""),
        "Recursos": " | ".join(p.get("recursos", [])), "Avaliações": " | ".join(p.get("avaliacoes", [])),
        "Observações": p.get("observacoes", ""), "Atualizado em": p.get("data_atualizacao", "")[:10],
    } for p in planejamentos]
    df = pd.DataFrame(linhas) if linhas else pd.DataFrame(columns=["Período", "Disciplina", "Objetivo Geral"])
    buffer = io.BytesIO()
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="Planejamento")
        ws = writer.sheets["Planejamento"]
        fill = PatternFill("solid", fgColor="1E3A8A")
        for cell in ws[1]:
            cell.font = Font(bold=True, color="FFD700")
            cell.fill = fill
            cell.alignment = Alignment(horizontal="center", vertical="center")
        for col in ws.columns:
            largura = max(len(str(c.value)) if c.value else 0 for c in col)
            ws.column_dimensions[col[0].column_letter].width = min(largura + 4, 60)
    return buffer.getvalue()


def exportar_pdf(aluno_nome: str, planejamentos: list) -> bytes | None:
    try:
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
        from reportlab.lib.units import cm
        from reportlab.lib import colors
        from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, PageBreak
    except ImportError:
        return None

    buffer = io.BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=A4, leftMargin=2 * cm, rightMargin=2 * cm,
                            topMargin=2 * cm, bottomMargin=2 * cm, title="Planejamento Pedagógico")
    styles = getSampleStyleSheet()
    azul = colors.HexColor("#1E3A8A")
    h1 = ParagraphStyle("H1", parent=styles["Heading1"], textColor=azul, fontSize=18, spaceAfter=10)
    h2 = ParagraphStyle("H2", parent=styles["Heading2"], textColor=azul, fontSize=14, spaceBefore=12, spaceAfter=6)
    body = ParagraphStyle("Body", parent=styles["BodyText"], fontSize=10, leading=14)
    P = lambda t, s=body: Paragraph(xml_esc(str(t)), s)
    lista = lambda itens: "<br/>".join("• " + xml_esc(str(i)) for i in itens) or "—"

    story = [P(f"Planejamento Pedagógico — {aluno_nome}", h1),
             P(f"SOS Exatas • Emissão: {datetime.now().strftime('%d/%m/%Y %H:%M')}"), Spacer(1, 0.5 * cm)]
    if not planejamentos:
        story.append(P("Nenhum planejamento cadastrado."))
    for i, p in enumerate(planejamentos):
        story.append(P(f"{p.get('disciplina', '—')} — {p.get('periodo', '—')}", h2))
        linhas = [
            ("Professor(a)", P(p.get("professor_responsavel") or "—")),
            ("Objetivo Geral", P(p.get("objetivo_geral") or "—")),
            ("Conteúdos", Paragraph(lista(p.get("conteudos", [])), body)),
            ("Metodologia", P(p.get("metodologia") or "—")),
            ("Recursos", Paragraph(lista(p.get("recursos", [])), body)),
            ("Avaliações", Paragraph(lista(p.get("avaliacoes", [])), body)),
            ("Observações", P(p.get("observacoes") or "—")),
            ("Atualizado em", P(p.get("data_atualizacao", "")[:10])),
        ]
        tabela = Table([[Paragraph(f"<b>{k}</b>", body), v] for k, v in linhas], colWidths=[4 * cm, 12 * cm])
        tabela.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#EFF3FB")),
            ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#B8C4E0")),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ]))
        story.append(tabela)
        if i < len(planejamentos) - 1:
            story.append(PageBreak())
    doc.build(story)
    return buffer.getvalue()


# ==============================================================================
# LOGO INSTITUCIONAL
# ==============================================================================
def exibir_logo_institucional(tamanho=180, centralizado=False):
    caminho = next((a for a in ["logo.png", "logo.jpg", "logo.jpeg"] if os.path.exists(a)), None)
    if caminho:
        if centralizado:
            _, c2, _ = st.columns([1, 2, 1])
            with c2:
                st.image(caminho, width=tamanho)
        else:
            st.image(caminho, width=tamanho)
        return
    just = "justify-content:center;" if centralizado else ""
    st.markdown(f"""
    <div style="display:flex; align-items:center; {just} gap:12px; margin-bottom:12px;">
        <svg width="44" height="44" viewBox="0 0 100 100" fill="none" xmlns="http://www.w3.org/2000/svg">
            <rect width="100" height="100" rx="22" fill="#1E3A8A"/>
            <path d="M50 15L78 31V69L50 85L22 69V31L50 15Z" stroke="#FFD700" stroke-width="6" stroke-linejoin="round"/>
            <circle cx="50" cy="50" r="14" fill="#D97706"/>
            <path d="M50 32V68M32 50H68" stroke="#ffffff" stroke-width="5" stroke-linecap="round"/>
        </svg>
        <div><span style="font-size:1.45rem; font-weight:900; color:#1E3A8A;">SOS EXATAS</span><br>
        <span style="font-size:.75rem; font-weight:700; color:#D97706; letter-spacing:1.5px; text-transform:uppercase;">Tutoria &amp; Alto Rendimento</span></div>
    </div>""", unsafe_allow_html=True)


# ==============================================================================
# MATERIAL OFICIAL SOS EXATAS (SUPABASE STORAGE & POSTGRESQL)
# ==============================================================================
SUPABASE_BUCKET = "materiais_oficiais"

MAT_DISCIPLINAS = ["Matemática", "Física", "Química", "Português",
                   "Redação", "Biologia", "Ciências", "História",
                   "Geografia", "Inglês"]
MAT_SERIES = ["6º Ano", "7º Ano", "8º Ano", "9º Ano",
              "1ª série EM", "2ª série EM", "3ª série EM",
              "Pré-Vestibular", "Geral"]
MAT_TIPOS = ["Apostila", "Lista de Exercícios", "Resumo", "Simulado",
             "Prova", "Gabarito", "Vídeo-aula", "Mapa Mental", "Outro"]
MAT_MODULOS = ["Módulo 1", "Módulo 2", "Módulo 3", "Módulo 4",
               "Revisão", "Extensivo", "Intensivo"]


def mat_listar_nuvem(filtros: dict = None) -> list:
    try:
        query = supabase.table("materiais_oficiais").select("*")
        if filtros:
            if filtros.get("disciplina") and filtros["disciplina"] != "Todas":
                query = query.eq("disciplina", filtros["disciplina"])
            if filtros.get("serie") and filtros["serie"] != "Todas":
                query = query.eq("serie", filtros["serie"])
            if filtros.get("tipo") and filtros["tipo"] != "Todos":
                query = query.eq("tipo", filtros["tipo"])
            if filtros.get("busca"):
                busca = filtros["busca"]
                query = query.or_(f"titulo.ilike.%{busca}%,descricao.ilike.%{busca}%,tags.ilike.%{busca}%")

        resp = query.order("criado_em", desc=True).execute()
        return resp.data or []
    except Exception as e:
        log.error(f"Erro ao listar materiais do Supabase: {e}")
        return []


def view_material_oficial(aluno, perfil, usuario):
    card("titulo-pp",
         "<h2>📚 Material Oficial SOS Exatas (Nuvem Supabase)</h2>"
         "<p>Biblioteca centralizada com apostilas, listas, simulados e materiais didáticos sincronizados na nuvem.</p>")
    st.caption("ℹ️ Módulo unificado ao Supabase Storage. Dados e arquivos protegidos contra perda em atualizações.")

    materiais = mat_listar_nuvem()
    k = st.columns(3)
    k[0].metric("📦 Total de Materiais", len(materiais))
    k[1].metric("📖 Disciplinas", len(set(m["disciplina"] for m in materiais)) if materiais else 0)
    k[2].metric("☁️ Armazenamento", "Supabase Storage (100% Persistente)")
    st.markdown("---")

    st.markdown("### ➕ Cadastrar Novo Material na Nuvem")
    with st.form("form_material_nuvem", clear_on_submit=True):
        c1, c2 = st.columns(2)
        with c1:
            titulo = st.text_input("Título *")
            disciplina = st.selectbox("Disciplina *", MAT_DISCIPLINAS)
            serie = st.selectbox("Série *", MAT_SERIES)
            tipo = st.selectbox("Tipo *", MAT_TIPOS)
        with c2:
            modulo = st.selectbox("Módulo", [""] + MAT_MODULOS)
            autor = st.text_input("Autor / Fonte")
            versao = st.text_input("Versão", value="1.0")
            tags = st.text_input("Tags (separadas por vírgula)")

        descricao = st.text_area("Descrição", height=80)
        st.markdown("**📎 Arquivo do Material (Salvo no Supabase Storage)**")
        arquivo = st.file_uploader(
            "Selecione o arquivo",
            type=["pdf", "docx", "doc", "xlsx", "pptx", "png", "jpg", "jpeg", "zip"],
            label_visibility="collapsed"
        )
        submit = st.form_submit_button("💾 Enviar para o Supabase Storage", type="primary", **W)

        if submit:
            if not titulo.strip():
                st.error("O campo **Título** é obrigatório.")
            elif not arquivo:
                st.error("É necessário anexar um arquivo.")
            else:
                try:
                    ext = Path(arquivo.name).suffix.lower().lstrip(".")
                    caminho_storage = _caminho_storage_seguro(disciplina, serie, arquivo.name)

                    file_bytes = arquivo.getvalue()
                    supabase.storage.from_(SUPABASE_BUCKET).upload(
                        path=caminho_storage,
                        file=file_bytes,
                        file_options={"content-type": arquivo.type or "application/octet-stream"}
                    )

                    u_nome = usuario["nome"] if isinstance(usuario, dict) else str(usuario)
                    supabase.table("materiais_oficiais").insert({
                        "titulo": titulo.strip(),
                        "descricao": descricao.strip(),
                        "disciplina": disciplina,
                        "serie": serie,
                        "tipo": tipo,
                        "modulo": modulo,
                        "tags": tags.strip(),
                        "autor": autor.strip(),
                        "versao": versao.strip() or "1.0",
                        "file_path": caminho_storage,
                        "file_size_kb": round(len(file_bytes) / 1024, 2),
                        "file_ext": ext,
                        "criado_por": u_nome
                    }).execute()

                    auditar("material_nuvem_criado", titulo.strip())
                    _salvar_e_recarregar("✅ Material enviado e salvo com sucesso na nuvem!")
                except Exception as e:
                    log.error(f"Erro no upload para o Supabase Storage: {e}")
                    st.error(f"Erro ao gravar arquivo na nuvem: {e}")

    st.markdown("---")
    st.markdown("### 🔍 Biblioteca de Materiais Cadastrados")
    if not materiais:
        st.info("📭 Nenhum material cadastrado na nuvem até o momento.")
    else:
        for m in materiais:
            with st.container(border=True):
                st.markdown(f"**{m['titulo']}** — {m['disciplina']} • {m['serie']} ({m['tipo']})")
                try:
                    url_publica = supabase.storage.from_(SUPABASE_BUCKET).get_public_url(m["file_path"])
                    st.markdown(f"[📥 Baixar Ficha]({url_publica})", unsafe_allow_html=True)
                except Exception:
                    pass


# ==============================================================================
# GESTÃO DE FICHAS (COM SELEÇÃO DIRETA DA BIBLIOTECA)
# ==============================================================================
def view_upload_fichas(aluno, perfil, usuario):
    d = aluno["dados"]
    st.title(f"Gestão de Fichas — {d['nome']}")
    
    materiais_nuvem = mat_listar_nuvem()
    
    with st.form("form_upload_ficha", clear_on_submit=True):
        origem = st.radio("Origem da Ficha:", ["Fazer Upload do Computador", "Selecionar da Biblioteca (Material Oficial)"], horizontal=True)
        
        profs = [p["nome"] for p in DB()["professores"]]
        resp = st.selectbox("Professor Responsável:", profs, index=profs.index(usuario["nome"]) if usuario["nome"] in profs else 0)
        
        material_selecionado = None
        arq = None
        tit = ""
        
        if origem == "Fazer Upload do Computador":
            tit = st.text_input("Título da Ficha")
            arq = st.file_uploader("Arquivo PDF:", type=["pdf", "docx", "doc", "xlsx", "pptx", "png", "jpg", "jpeg", "zip"])
        else:
            if not materiais_nuvem:
                st.warning("📭 Nenhum material cadastrado na Biblioteca Oficial ainda.")
            else:
                material_selecionado = st.selectbox(
                    "Selecione o material da biblioteca:",
                    materiais_nuvem,
                    format_func=lambda m: f"{m['titulo']} ({m['disciplina']} • {m['serie']} — {m['tipo']})"
                )
                if material_selecionado:
                    tit = material_selecionado["titulo"]
                    
        if st.form_submit_button("Publicar Ficha para o Aluno"):
            if origem == "Fazer Upload do Computador":
                if tit.strip() and arq:
                    nome = salvar_upload(arq, PASTA_FICHAS, d["id"])
                    aluno["fichas_disponibilizadas"].append({
                        "nome_arquivo": nome,
                        "titulo": tit.strip(),
                        "data_upload": str(date.today()),
                        "uploaded_by": resp,
                        "is_nuvem": False
                    })
                    notificar(d["id"], "📄 Nova ficha disponível", tit.strip(), origem="professor")
                    auditar("ficha_publicada", f"{d['id']} {nome}")
                    _salvar_e_recarregar("Ficha publicada com sucesso!")
                else:
                    st.error("Informe o título e anexe o arquivo.")
            else:
                if material_selecionado:
                    aluno["fichas_disponibilizadas"].append({
                        "nome_arquivo": material_selecionado["file_path"],
                        "file_path": material_selecionado["file_path"],
                        "titulo": material_selecionado["titulo"],
                        "data_upload": str(date.today()),
                        "uploaded_by": resp,
                        "is_nuvem": True
                    })
                    notificar(d["id"], "📄 Nova ficha disponível", material_selecionado["titulo"], origem="professor")
                    auditar("ficha_publicada_nuvem", f"{d['id']} {material_selecionado['titulo']}")
                    _salvar_e_recarregar("Ficha da Biblioteca vinculada com sucesso!")
                else:
                    st.error("Selecione um material válido da biblioteca.")


def view_fichas_aluno(aluno, perfil, usuario):
    st.title("Central de Fichas de Estudo")
    fichas = aluno.get("fichas_disponibilizadas", [])
    if not fichas:
        st.info("Nenhuma ficha disponibilizada no momento.")
    for i, f in enumerate(fichas):
        with st.container(border=True):
            a, b = st.columns([3, 1])
            a.markdown(f"📄 **{esc(f['titulo'])}**")
            a.caption(f"Disponibilizado por {esc(f['uploaded_by'])} em {f['data_upload']}")
            with b:
                if f.get("is_nuvem") or f.get("file_path"):
                    try:
                        path_or_url = f.get("file_path") or f.get("nome_arquivo")
                        url = supabase.storage.from_(SUPABASE_BUCKET).get_public_url(path_or_url)
                        st.markdown(f"[📥 Baixar Ficha]({url})", unsafe_allow_html=True)
                    except Exception:
                        st.caption("Arquivo indisponível")
                else:
                    botao_download("⬇️ Baixar Ficha", PASTA_FICHAS, f["nome_arquivo"], f"dl_{i}")


# ==============================================================================
# NOVOS MÓDULOS: CONTRATOS & AGENDA
# ==============================================================================
def view_contratos(aluno, perfil, usuario):
    st.title("📄 Gestão de Contratos Acadêmicos")
    eh_gestor = pode(perfil, "gerenciar_contratos") or perfil in (Perfil.ADMIN, Perfil.COORDENADOR)

    if eh_gestor and aluno:
        d = aluno["dados"]
        st.subheader(f"Contratos de: {d['nome']} ({d['id']})")
        contratos_aluno = aluno.setdefault("contratos", [])

        with st.expander("➕ Adicionar Novo Contrato"):
            with st.form("form_novo_contrato"):
                c1, c2 = st.columns(2)
                num_c = c1.text_input("Número do Contrato", value=f"SOS-2026-{uuid.uuid4().hex[:4].upper()}")
                valor_c = c2.number_input("Valor Total (R$)", 0.0, 50000.0, 4500.0, 100.0)
                d_inicio = c1.date_input("Data de Início", date.today())
                d_fim = c2.date_input("Data de Término", date.today() + timedelta(days=180))
                status_c = c1.selectbox("Status", ["Vigente", "Encerrado", "Pendente de Assinatura", "Renovação Necessária"])
                pag_c = c2.selectbox("Forma de Pagamento", ["Boleto / Pix Mensal", "Cartão de Crédito", "À Vista"])
                obs_c = st.text_area("Observações Contratuais")

                if st.form_submit_button("Salvar Contrato"):
                    contratos_aluno.append({
                        "id": novo_id("CONT"),
                        "numero_contrato": num_c.strip(),
                        "data_inicio": str(d_inicio),
                        "data_fim": str(d_fim),
                        "status": status_c,
                        "valor_total": float(valor_c),
                        "forma_pagamento": pag_c,
                        "observacoes": obs_c.strip()
                    })
                    auditar("contrato_criado", f"{d['id']} {num_c}")
                    _salvar_e_recarregar("Contrato cadastrado com sucesso!")

        if contratos_aluno:
            rows = []
            for c in contratos_aluno:
                rows.append({
                    "Nº Contrato": c.get("numero_contrato"),
                    "Início": c.get("data_inicio"),
                    "Término": c.get("data_fim"),
                    "Status": c.get("status"),
                    "Valor (R$)": f"R$ {c.get('valor_total', 0):,.2f}",
                    "Pagamento": c.get("forma_pagamento")
                })
            st.dataframe(pd.DataFrame(rows), hide_index=True, **W)
        else:
            st.info("Nenhum contrato cadastrado para este estudante.")
    else:
        st.info("Visão Geral de Contratos e Termos Acadêmicos do SOS Exatas.")
        if aluno and aluno.get("contratos"):
            for c in aluno["contratos"]:
                with st.container(border=True):
                    st.markdown(f"**Contrato Nº:** `{c.get('numero_contrato')}` — **Status:** {c.get('status')}")
                    st.write(f"Período: {c.get('data_inicio')} até {c.get('data_fim')} | Valor: R$ {c.get('valor_total', 0):,.2f}")
                    if c.get("observacoes"):
                        st.caption(f"Obs: {c.get('observacoes')}")


def view_agenda(aluno, perfil, usuario):
    st.title("📅 Agenda & Calendário de Atendimentos")
    agenda_global = DB().setdefault("agenda", [])

    eh_gestor = pode(perfil, "gerenciar_agenda") or perfil in (Perfil.ADMIN, Perfil.COORDENADOR, Perfil.PROFESSOR)

    if eh_gestor:
        with st.expander("➕ Agendar Novo Compromisso / Aula"):
            with st.form("form_nova_agenda", clear_on_submit=True):
                c1, c2 = st.columns(2)
                titulo_ag = c1.text_input("Título do Compromisso *")
                tipo_ag = c2.selectbox("Tipo", ["Aula 1:1", "Mentoria Coletiva", "Banca de Estudos", "Reunião Pedagógica", "Simulado"])
                data_ag = c1.date_input("Data", date.today())
                hora_ag = c2.text_input("Horário (HH:MM)", value="14:00")
                local_ag = c1.text_input("Local ou Link Virtual", value="Sede SOS Exatas / Sala Presencial")
                resp_ag = c2.text_input("Responsável / Professor", value=usuario.get("nome", "Coordenação"))
                part_ag = st.text_input("Participantes / Turma Alvo", value="Geral")

                if st.form_submit_button("Agendar na Agenda Global"):
                    if not titulo_ag.strip():
                        st.error("Informe o título do compromisso.")
                    else:
                        agenda_global.append({
                            "id": novo_id("AG"),
                            "titulo": titulo_ag.strip(),
                            "data": str(data_ag),
                            "horario": hora_ag.strip(),
                            "local_ou_link": local_ag.strip(),
                            "responsavel": resp_ag.strip(),
                            "tipo": tipo_ag,
                            "participantes": part_ag.strip()
                        })
                        auditar("agenda_criada", titulo_ag.strip())
                        _salvar_e_recarregar("Compromisso agendado com sucesso!")

    st.subheader("🗓️ Próximos Eventos e Aulas")
    if not agenda_global:
        st.info("Nenhum evento agendado no momento.")
    else:
        agenda_ordenada = sorted(agenda_global, key=lambda x: (x.get("data", ""), x.get("horario", "")))
        for ev in agenda_ordenada:
            with st.container(border=True):
                col1, col2 = st.columns([4, 1])
                with col1:
                    st.markdown(f"**📌 {esc(ev['titulo'])}** ({esc(ev.get('tipo', 'Evento'))})")
                    st.write(f"📅 **Data:** {ev.get('data')} às {ev.get('horario')} | 👤 **Responsável:** {esc(ev.get('responsavel', '—'))}")
                    st.write(f"📍 **Local/Link:** {esc(ev.get('local_ou_link', '—'))} | 👥 **Participantes:** {esc(ev.get('participantes', '—'))}")
                with col2:
                    if eh_gestor and st.button("🗑️ Excluir", key=f"del_ev_{ev['id']}"):
                        agenda_global.remove(ev)
                        auditar("agenda_excluida", ev['id'])
                        _salvar_e_recarregar("Evento removido!")


# ==============================================================================
# VIEWS DO SISTEMA
# ==============================================================================
def view_planejamento(aluno, perfil, usuario):
    d = aluno["dados"]
    aid = d["id"]
    card("titulo-pp", f"<h2>📚 Planejamento Pedagógico e Situação Escolar</h2>"
                     f"<p>Objetivos, avaliações, notas e situação pedagógica de <b>{esc(d['nome'])}</b>.</p>")
    renderizar_widget_notificacoes(aid)

    periodos = periodos_disponiveis(aluno)
    disciplinas = disciplinas_disponiveis()
    c1, c2, c3 = st.columns([2, 2, 1])
    per_f = c1.selectbox("Período", ["Todos"] + periodos)
    dis_f = c2.selectbox("Disciplina", ["Todas"] + disciplinas)
    with c3:
        st.write("")
        st.write("")
        if pode(perfil, "criar") and st.button("➕ Novo Plano", **W):
            st.session_state["pp_idx"] = -1
            st.rerun()

    todos = aluno.get("planejamentos_pedagogicos", [])
    filtrados = [(i, p) for i, p in enumerate(todos)
                 if (per_f == "Todos" or p.get("periodo") == per_f)
                 and (dis_f == "Todas" or p.get("disciplina") == dis_f)]
    planos = [p for _, p in filtrados]

    k = st.columns(4)
    k[0].metric("📋 Planos", len(planos))
    k[1].metric("📖 Disciplinas", len({p.get("disciplina") for p in planos}))
    k[2].metric("📅 Períodos", len({p.get("periodo") for p in planos}))
    k[3].metric("✅ Avaliações", sum(len(p.get("avaliacoes", [])) for p in planos))

    if pode(perfil, "exportar"):
        e1, e2, _ = st.columns([1, 1, 4])
        base = re.sub(r"\W+", "_", d["nome"])
        xls = exportar_excel(planos)
        pdf = exportar_pdf(d["nome"], planos)
        if xls:
            e1.download_button("⬇️ Baixar Excel", xls, f"planejamento_{base}.xlsx",
                               "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", **W)
        if pdf:
            e2.download_button("⬇️ Baixar PDF", pdf, f"planejamento_{base}.pdf", "application/pdf", **W)
    st.markdown("---")

    idx_edit = st.session_state.get("pp_idx")
    if idx_edit is not None and pode(perfil, "editar"):
        ed = todos[idx_edit] if 0 <= idx_edit < len(todos) else {}
        st.subheader("✏️ Editar Planejamento" if ed else "➕ Novo Planejamento")
        with st.form("form_planejamento"):
            a, b = st.columns(2)
            with a:
                per_opts = periodos_disponiveis(aluno)
                per_c = st.selectbox("Período *", per_opts,
                                     index=per_opts.index(ed["periodo"]) if ed.get("periodo") in per_opts else 0)
                dis_opts = disciplinas if ed.get("disciplina") in disciplinas or not ed else disciplinas + [ed["disciplina"]]
                dis_c = st.selectbox("Disciplina *", dis_opts,
                                     index=dis_opts.index(ed["disciplina"]) if ed.get("disciplina") in dis_opts else 0)
            with b:
                prof_c = st.text_input("Professor(a) Responsável", value=ed.get("professor_responsavel", usuario["nome"]))
                obj_c = st.text_area("Objetivo Geral *", value=ed.get("objetivo_geral", ""), height=80)
            cont = st.text_area("Conteúdos (um por linha)", "\n".join(ed.get("conteudos", [])), height=90)
            meto = st.text_area("Metodologia", ed.get("metodologia", ""), height=70)
            rec = st.text_area("Recursos (um por linha)", "\n".join(ed.get("recursos", [])), height=70)
            ava = st.text_area("Avaliações (uma por linha)", "\n".join(ed.get("avaliacoes", [])), height=70)
            obs = st.text_area("Observações / Situação Escolar", ed.get("observacoes", ""), height=60)
            s1, s2 = st.columns(2)
            salvou = s1.form_submit_button("💾 Salvar Planejamento", type="primary", **W)
            cancelou = s2.form_submit_button("❌ Cancelar", **W)

        if cancelou:
            st.session_state["pp_idx"] = None
            st.rerun()
        if salvou:
            if not obj_c.strip():
                st.error("O Objetivo Geral é obrigatório.")
            else:
                novo = {"periodo": per_c, "disciplina": dis_c, "professor_responsavel": prof_c.strip(),
                        "objetivo_geral": obj_c.strip(),
                        "conteudos": [x.strip() for x in cont.splitlines() if x.strip()],
                        "metodologia": meto.strip(),
                        "recursos": [x.strip() for x in rec.splitlines() if x.strip()],
                        "avaliacoes": [x.strip() for x in ava.splitlines() if x.strip()],
                        "observacoes": obs.strip(),
                        "data_criacao": ed.get("data_criacao", str(date.today())),
                        "data_atualizacao": str(date.today()), "anexos": ed.get("anexos", [])}
                if ed:
                    todos[idx_edit] = novo
                else:
                    todos.append(novo)
                auditar("planejamento_salvo", f"{aid} {dis_c} {per_c}")
                st.session_state["pp_idx"] = None
                _salvar_e_recarregar("Planejamento gravado!")

    if not planos:
        st.info("📭 Nenhum planejamento para os filtros selecionados.")
    for pos, (i_real, p) in enumerate(filtrados):
        with st.expander(f"📘 {p.get('disciplina')} • {p.get('periodo')}", expanded=(pos == 0)):
            ca, cb = st.columns([3, 1])
            ca.markdown(f"**🎯 Objetivo Geral:** {esc(p.get('objetivo_geral', ''))}")
            cb.markdown(f"**👨‍🏫 Responsável:** {esc(p.get('professor_responsavel', ''))}")
            cb.caption(f"Atualizado em: {p.get('data_atualizacao', '')[:10]}")


def view_diretorio(aluno, perfil, usuario):
    st.title("Diretório Geral de Gestão — SOS Exatas")
    alunos = DB()["alunos"]
    eh_admin = (perfil == Perfil.ADMIN)

    t1, t2, t3 = st.tabs(["👨‍🎓 Alunos", "👪 Famílias & Responsáveis", "👨‍🏫 Professores & Tutores"])

    with t1:
        if alunos:
            rows = []
            for aid, a in alunos.items():
                d, op = a["dados"], a["operacao"]
                saldo = op["horas_contratadas"] - op["horas_realizadas"]
                rows.append({
                    "Matrícula": aid,
                    "Nome": d["nome"],
                    "Modalidade": d.get("modalidade", ""),
                    "Escola / Série": f"{d.get('escola', 'N/D')} ({d.get('serie', 'N/D')})",
                    "Contratadas": f"{op['horas_contratadas']:.1f}h",
                    "Realizadas": f"{op['horas_realizadas']:.1f}h",
                    "Saldo": f"{saldo:.1f}h",
                    "Status": "🚨 Crítico (Renovar)" if saldo <= 3 else "🟢 Regular"
                })
            st.dataframe(pd.DataFrame(rows), hide_index=True, **W)
        else:
            st.info("Nenhum estudante matriculado.")

    with t2:
        msgs = DB().get("mensagens_familias", [])
        rows_f = [{"Aluno": a["dados"]["nome"], "Matrícula": aid,
                   "Responsáveis": a["dados"].get("responsaveis", ""), "Contato": a["dados"].get("contato", "N/D"),
                   "Mensagens": sum(1 for m in msgs if m.get("aluno_id") == aid)} for aid, a in alunos.items()]
        if rows_f:
            st.dataframe(pd.DataFrame(rows_f), hide_index=True, **W)
        else:
            st.info("Nenhuma família cadastrada.")

    with t3:
        profs = DB().get("professores", [])
        rows_p = []
        for p in profs:
            ats = [s for al in alunos.values() for s in al.get("atendimentos_processo", []) if s.get("professor") == p["nome"]]
            horas = sum(s.get("duracao_h", 1.5) for s in ats)
            rows_p.append({"Código": p["id"], "Nome": p["nome"], "Disciplina": p["disciplina"], "Contato": p["contato"],
                           "Valor Hora": f"R$ {p.get('valor_hora', 90):.2f}", "Atendimentos": len(ats),
                           "Horas": f"{horas:.1f}h"})
        if rows_p:
            st.dataframe(pd.DataFrame(rows_p), hide_index=True, **W)
        else:
            st.info("Nenhum professor cadastrado.")


def view_cad_professor(aluno, perfil, usuario):
    st.title("Cadastro de Novo Professor / Tutor")
    profs = DB()["professores"]
    ids = [int(p["id"].split("-")[1]) for p in profs if re.fullmatch(r"PROF-\d+", p["id"])]
    prox = f"PROF-{(max(ids) + 1) if ids else 1:02d}"
    with st.form("form_cad_professor", clear_on_submit=True):
        a, b = st.columns(2)
        with a:
            st.text_input("Código do Docente", value=prox, disabled=True)
            nome = st.text_input("Nome Completo")
            disc = st.selectbox("Disciplina Principal", disciplinas_disponiveis())
            contato = st.text_input("Telefone / WhatsApp", placeholder="(81) 98888-7777")
        with b:
            valor = st.number_input("Valor por Hora (R$/h)", 30.0, 300.0, 90.0, 5.0)
            login = st.text_input("Login de Acesso").strip().lower()
            senha = st.text_input("Senha Inicial", type="password")
        if st.form_submit_button("Concluir Cadastro"):
            erro = senha_valida(senha) if senha else "Informe a senha."
            if not nome.strip() or not login:
                st.error("Preencha nome e login.")
            elif login in DB()["usuarios"]:
                st.error("Este login já existe.")
            elif erro:
                st.error(erro)
            else:
                profs.append({"id": prox, "nome": nome.strip(), "disciplina": disc, "valor_hora": float(valor),
                            "contato": contato.strip() or "(81) 99999-0000"})
                DB()["usuarios"][login] = {"nome": nome.strip(), "hash_senha": gerar_hash(senha),
                                           "perfil": "professor", "aluno_vinculado": None, "trocar_senha": True}
                auditar("professor_cadastrado", f"{prox} {login}")
                _salvar_e_recarregar(f"Professor cadastrado. Login '{login}' ativado.")


def view_cockpit(aluno, perfil, usuario):
    d = aluno["dados"]
    st.title(f"Relatório do Acompanhamento — {d['nome']}")

    venc = ciclos_vencidos(aluno)
    if venc:
        st.error(f"⏰ {len(venc)} ciclo(s) com prazo vencido: " + ", ".join(c["id"] for c in venc))

    st.subheader("📝 Lançamento do Acompanhamento")
    habs = DB()["habilidades_cadastradas"]

    with st.form("form_sessao_prof", clear_on_submit=True):
        f1, f2 = st.columns(2)
        with f1:
            dt = st.date_input("Data", date.today())
            profs = [p["nome"] for p in DB()["professores"]]
            idx = profs.index(usuario["nome"]) if usuario["nome"] in profs else 0
            prof = st.selectbox("Mediador", profs, index=idx)
            dur = st.number_input("Duração (horas)", 0.25, 6.0, 1.5, 0.25)
        with f2:
            tema = st.text_input("Tema / Situação-Problema *")
            pre = st.number_input("Desempenho Pré-Aula %", 0.0, 100.0, 40.0, 5.0)
            pos = st.number_input("Desempenho Pós-Aula %", 0.0, 100.0, 65.0, 5.0)

        st.markdown("---")
        habs_sel = st.multiselect(
            "Habilidades Foco *",
            [h["codigo"] for h in habs],
            format_func=lambda x: f"{x} - {next((h['descricao'][:45] for h in habs if h['codigo'] == x), '')}..."
        )
        blocos_sel = st.multiselect("Blocos SOS *", list(TOPICOS_SOS), format_func=lambda x: f"{x} - {TOPICOS_SOS[x].nome}")
        obstaculos_sel = st.multiselect("Obstáculos Principais *", list(TAXONOMIA_ERRO_PEDAGOGICO), default=["Modelagem"])

        st.markdown("---")
        presc = st.text_area("Recomendação para o Estudante *", "Resolver ficha estruturante correspondente.")
        obs_fam = st.text_area("Recomendações para a família", "Incentivar a resolução espaçada.")

        if st.form_submit_button("Gravar Relatório de Acompanhamento", type="primary", **W):
            if not tema.strip():
                st.error("Informe o tema da sessão.")
            elif not habs_sel:
                st.error("Selecione ao menos uma Habilidade Foco.")
            else:
                novo_at = {
                    "id": novo_id("SESS"), "data": str(dt), "professor": prof,
                    "habilidades_cod": habs_sel, "habilidade_cod": ", ".join(habs_sel),
                    "topicos_sos": blocos_sel, "topico_sos": ", ".join(blocos_sel),
                    "tema": tema.strip(), "tipos_erro": obstaculos_sel, "tipo_erro": ", ".join(obstaculos_sel),
                    "avaliacao_pre": pre, "avaliacao_pos": pos, "ganho_ipsativo": pos - pre,
                    "duracao_h": float(dur), "observacao": obs_fam.strip(), "prescricao": presc.strip()
                }
                aluno["atendimentos_processo"].append(novo_at)
                aluno["operacao"]["horas_realizadas"] += float(dur)
                aluno["operacao"]["presencas"] += 1
                auditar("atendimento_lancado", f"{d['id']} {tema.strip()} {dur}h")
                _salvar_e_recarregar("Relatório de Acompanhamento gravado com sucesso!")


def view_perfil_aluno(aluno, perfil, usuario):
    d = aluno["dados"]
    st.title(f"Perfil do Estudante — {esc(d['nome'])}")
    with st.container(border=True):
        st.write(f"**Matrícula:** `{esc(d['id'])}`")
        st.write(f"**Modalidades:** {esc(', '.join(d.get('modalidades', [d.get('modalidade', '')])))}")
        st.write(f"**Escola / Série:** {esc(d.get('escola', ''))} ({esc(d.get('serie', ''))})")
        st.write(f"**Objetivo:** {esc(d.get('objetivo_estudante', ''))}")


def view_diario(aluno, perfil, usuario):
    st.title("Diário de Bordo: Minha Reflexão de Estudo")
    with st.form("form_diario", clear_on_submit=True):
        tarefa = st.text_input("Qual lista ou ficha você resolveu hoje?")
        sent = st.select_slider("Como você se sentiu?", ["Muito travado", "Com dúvidas", "Confiante", "Pleno domínio"])
        duv = st.text_area("Pergunta para o professor:")
        if st.form_submit_button("Salvar e Enviar"):
            if tarefa.strip():
                aluno["autoavaliacoes_estudante"].append({"data": str(date.today()), "tarefa": tarefa.strip(), "sentimento": sent, "duvida": duv})
                _salvar_e_recarregar("Reflexão gravada!")
            else:
                st.error("Informe a tarefa.")


def view_revisoes(aluno, perfil, usuario):
    st.title("Minhas Revisões Programadas")
    revs = aluno.get("revisoes_agendadas", [])
    if not revs:
        st.info("Nenhuma revisão programada no momento.")
    for i, r in enumerate(revs):
        with st.container(border=True):
            st.markdown(f"**{esc(r['tema'])}**")


def view_relatorio_familia(aluno, perfil, usuario):
    d = aluno["dados"]
    op = aluno["operacao"]
    card("timbrado-institucional", f"""
        <div class="timbrado-header">
            <div><h2 style="color:#1E3A8A;margin:0;">SOS EXATAS TUTORIA &amp; VEST</h2>
            <span style="font-size:.85rem;color:#475569;">Tavares e Feitosa Serviços de Ensino LTDA</span></div>
        </div>
        <p><strong>Estudante:</strong> {esc(d['nome'])} ({esc(d['id'])})</p>
        <p><strong>Carga horária realizada:</strong> {op['horas_realizadas']:.1f}h de {op['horas_contratadas']:.1f}h.</p>
    """)


def view_mensagens_familia(aluno, perfil, usuario):
    st.title("Canal Direto com a Coordenação")
    st.info("Envie mensagens diretamente à coordenação pedagógica.")


def view_intervencoes(aluno, perfil, usuario):
    st.title("Relatório das Aulas Anteriores")
    atendimentos = aluno.get("atendimentos_processo", [])
    if not atendimentos:
        st.info("Nenhuma aula anterior registrada.")
    for at in reversed(atendimentos):
        with st.container(border=True):
            st.markdown(f"**{esc(at.get('tema', ''))}** — {esc(at.get('data', ''))}")


def view_materiais_alunos(aluno, perfil, usuario):
    st.title("Materiais Enviados pelos Alunos")
    st.info("Gestão de envios acadêmicos.")


def view_docs_coord(aluno, perfil, usuario):
    st.title("Comunicação Docente com a Coordenação")


def view_matriz(aluno, perfil, usuario):
    st.title("Matriz Curricular: BNCC & SOS Exatas")
    st.dataframe(pd.DataFrame(DB()["habilidades_cadastradas"]), hide_index=True, **W)


def view_painel360(aluno, perfil, usuario):
    d, op = aluno["dados"], aluno["operacao"]
    st.title(f"Painel 360° — {esc(d['nome'])}")
    c1, c2 = st.columns(2)
    c1.metric("Horas Realizadas", f"{op['horas_realizadas']:.1f} h")
    c2.metric("Presenças", f"{op['presencas']}")


def view_caixa(aluno, perfil, usuario):
    st.title("Caixa de Entrada da Coordenação")


def view_produtividade(aluno, perfil, usuario):
    st.title("Produtividade da Equipe Pedagógica")
    rows = []
    for p in DB().get("professores", []):
        ats = [s for al in DB()["alunos"].values() for s in al.get("atendimentos_processo", []) if s.get("professor") == p["nome"]]
        h = sum(s.get("duracao_h", 1.5) for s in ats)
        rows.append({"Professor": p["nome"], "Disciplina": p["disciplina"], "Aulas": len(ats),
                     "Horas": round(h, 2), "Repasse Estimado (R$)": round(h * p.get("valor_hora", 90), 2)})
    st.dataframe(pd.DataFrame(rows), hide_index=True, **W)


def view_matricula(aluno, perfil, usuario):
    st.title("Matrícula de Novo Estudante")


def view_exclusao(aluno, perfil, usuario):
    st.title("Exclusão e Desligamento de Alunos")


def view_usuarios(aluno, perfil, usuario):
    st.title("Gerenciador de Acessos & Logins")


def view_auditoria(aluno, perfil, usuario):
    st.title("Log de Auditoria")
    log_reg = DB().get("auditoria", [])
    if log_reg:
        st.dataframe(pd.DataFrame(log_reg[::-1][:300]), hide_index=True, **W)


# ==============================================================================
# ROTEAMENTO E ESTRUTURA MODULAR DE NAVEGAÇÃO
# ==============================================================================
VIEWS = {
    "📥 Fichas de Estudo (Download)": (view_fichas_aluno, "visualizar", True),
    "📚 Meu Planejamento Pedagógico": (view_planejamento, "visualizar", True),
    "👤 Meu Perfil, Envios & Histórico": (view_perfil_aluno, "enviar_material", True),
    "💭 Diário de Bordo Metacognitivo": (view_diario, "responder_diario", True),
    "⏳ Minhas Revisões Espaçadas": (view_revisoes, "responder_diario", True),
    "👪 Relatório de Acompanhamento Mensal": (view_relatorio_familia, "visualizar", True),
    "📚 Planejamento Pedagógico & Boletim": (view_planejamento, "visualizar", True),
    "✉️ Mensagens para a Coordenação": (view_mensagens_familia, "comentar", True),
    "📝 Relatório do Acompanhamento": (view_cockpit, "lancar_atendimento", True),
    "📚 Planejamento Pedagógico e Situação Escolar": (view_planejamento, "visualizar", True),
    "📁 Upload de Fichas de Estudo": (view_upload_fichas, "anexar", True),
    "📜 Relatório das Aulas Anteriores": (view_intervencoes, "lancar_atendimento", True),
    "📥 Materiais Enviados pelos Alunos": (view_materiais_alunos, "lancar_atendimento", True),
    "📎 Enviar Documentos à Coordenação": (view_docs_coord, "anexar", False),
    "🧩 Matriz Curricular (BNCC & SOS)": (view_matriz, "visualizar", False),
    "👤 Painel 360° do Aluno": (view_painel360, "lancar_atendimento", True),
    "📋 Diretório Geral": (view_diretorio, "matricular", False),
    "👤 Painel 360° & Gatilho Financeiro": (view_painel360, "gerenciar_matriz", True),
    "✉️ Mensagens & Documentos": (view_caixa, "gerenciar_matriz", False),
    "🧩 Gestão da Matriz BNCC": (view_matriz, "gerenciar_matriz", False),
    "👥 Equipe & Produtividade": (view_produtividade, "gerenciar_matriz", False),
    "➕ Matrícula de Novo Estudante": (view_matricula, "matricular", False),
    "👨‍🏫 Cadastrar Novo Professor": (view_cad_professor, "cadastrar_professor", False),
    "🗑️ Exclusão de Estudantes": (view_exclusao, "excluir_aluno", False),
    "🔐 Gerenciador de Usuários & Logins": (view_usuarios, "gerenciar_usuarios", False),
    "🕵️ Log de Auditoria": (view_auditoria, "auditoria", False),
    "📚 Material Oficial SOS Exatas": (view_material_oficial, "visualizar", False),
    "📚 Material Oficial — Gerenciar": (view_material_oficial, "criar", False),
    "📚 Material Oficial SOS Exatas (Doc)": (view_material_oficial, "anexar", False),
    "📄 Gestão de Contratos": (view_contratos, "gerenciar_contratos", True),
    "📅 Agenda & Calendário": (view_agenda, "gerenciar_agenda", False),
}

MODULOS_ADMIN = {
    "📄 Produzir Relatório de Acompanhamento": [
        "📝 Relatório do Acompanhamento",
        "📜 Relatório das Aulas Anteriores",
        "📚 Planejamento Pedagógico e Situação Escolar",
    ],
    "🏫 Gestão Geral & Comunidade": [
        "📋 Diretório Geral",
        "👤 Painel 360° & Gatilho Financeiro",
        "✉️ Mensagens & Documentos",
        "📄 Gestão de Contratos",
    ],
    "📁 Materiais & Conteúdos": [
        "📁 Upload de Fichas de Estudo",
        "📥 Materiais Enviados pelos Alunos",
        "🧩 Gestão da Matriz BNCC",
    ],
    "📚 Material Oficial SOS Exatas": [
        "📚 Material Oficial — Gerenciar",
    ],
    "👥 Equipe & Produtividade": [
        "👥 Equipe & Produtividade",
        "👨‍🏫 Cadastrar Novo Professor",
    ],
    "📅 Agenda & Compromissos": [
        "📅 Agenda & Calendário",
    ],
    "⚙️ Administração & Acessos": [
        "➕ Matrícula de Novo Estudante",
        "🗑️ Exclusão de Estudantes",
        "🔐 Gerenciador de Usuários & Logins",
        "🕵️ Log de Auditoria",
    ],
}

MODULOS_PROFESSOR = {
    "📄 Produzir Relatório de Acompanhamento": [
        "📝 Relatório do Acompanhamento",
        "📜 Relatório das Aulas Anteriores",
        "📚 Planejamento Pedagógico e Situação Escolar",
    ],
    "📁 Materiais & Conteúdos": [
        "📁 Upload de Fichas de Estudo",
        "📥 Materiais Enviados pelos Alunos",
        "🧩 Matriz Curricular (BNCC & SOS)",
    ],
    "📚 Material Oficial SOS Exatas": [
        "📚 Material Oficial SOS Exatas (Doc)",
    ],
    "👤 Aluno & Comunicação": [
        "👤 Painel 360° do Aluno",
        "📎 Enviar Documentos à Coordenação",
    ],
    "📅 Agenda & Compromissos": [
        "📅 Agenda & Calendário",
    ],
}

MENU_SIMPLES = {
    Perfil.ALUNO: [
        "📥 Fichas de Estudo (Download)",
        "📚 Meu Planejamento Pedagógico",
        "📚 Material Oficial SOS Exatas",
        "👤 Meu Perfil, Envios & Histórico",
        "💭 Diário de Bordo Metacognitivo",
        "⏳ Minhas Revisões Espaçadas",
        "📅 Agenda & Calendário",
    ],
    Perfil.FAMILIA: [
        "👪 Relatório de Acompanhamento Mensal",
        "📚 Planejamento Pedagógico & Boletim",
        "📚 Material Oficial SOS Exatas",
        "✉️ Mensagens para a Coordenação",
        "📄 Gestão de Contratos",
        "📅 Agenda & Calendário",
    ],
}


# ==============================================================================
# LOGIN, SESSÃO E TROCA DE SENHA
# ==============================================================================
def logout():
    for k in ["autenticado", "usuario_key", "ultimo_acesso", "pp_idx", "db"]:
        st.session_state.pop(k, None)


def tela_login():
    _, c2, _ = st.columns([1, 1.3, 1])
    with c2:
        with st.container(border=True):
            exibir_logo_institucional(190, True)
            st.markdown("<h3 style='color:#1E3A8A;text-align:center;'>Portal de Aprendizagem & Ensino</h3>", unsafe_allow_html=True)
            st.caption("Autenticação segura • Processo de Acompanhamento Extensivo")
            with st.form("form_login"):
                usuario = st.text_input("Usuário").strip().lower()
                senha = st.text_input("Senha", type="password")
                if st.form_submit_button("Acessar o Sistema"):
                    bloq = DB()["bloqueios"].get(usuario)
                    u = DB()["usuarios"].get(usuario)

                    _HASH_FALSO = "pbkdf2$" + "0" * 32 + "$" + "0" * 64
                    hash_alvo = u["hash_senha"] if u else _HASH_FALSO
                    senha_ok = verificar_senha(senha, hash_alvo)

                    if bloq and datetime.fromisoformat(bloq["ate"]) > datetime.now():
                        st.error("Muitas tentativas. Aguarde alguns minutos e tente novamente.")
                    elif u and senha_ok:
                        DB()["bloqueios"].pop(usuario, None)
                        st.session_state.usuario_key = usuario
                        st.session_state.autenticado = True
                        st.session_state.ultimo_acesso = datetime.now()
                        auditar("login", usuario)
                        _salvar_e_recarregar("Login efetuado.")
                    else:
                        if u:
                            b = DB()["bloqueios"].setdefault(usuario, {"falhas": 0, "ate": "2000-01-01T00:00:00"})
                            b["falhas"] += 1
                            if b["falhas"] >= MAX_FALHAS:
                                b["ate"] = (datetime.now() + timedelta(minutes=BLOQUEIO_MIN)).isoformat()
                                b["falhas"] = 0
                            try:
                                salvar_banco()
                            except Exception:
                                pass
                        st.error("Usuário ou senha inválidos.")
    _render_flash()
    st.stop()


def tela_troca_senha(usuario_key: str):
    st.title("🔐 Defina uma nova senha")
    st.info("Por segurança, é necessário trocar a senha inicial antes de continuar.")
    with st.form("form_troca"):
        atual = st.text_input("Senha atual", type="password")
        nova = st.text_input("Nova senha", type="password")
        conf = st.text_input("Confirmar nova senha", type="password")
        if st.form_submit_button("Salvar nova senha"):
            u = DB()["usuarios"][usuario_key]
            erro = senha_valida(nova)
            if not verificar_senha(atual, u["hash_senha"]):
                st.error("Senha atual incorreta.")
            elif nova != conf:
                st.error("A confirmação não confere.")
            elif nova == atual:
                st.error("A nova senha deve ser diferente da atual.")
            elif erro:
                st.error(erro)
            else:
                u["hash_senha"] = gerar_hash(nova)
                u["trocar_senha"] = False
                auditar("senha_trocada", usuario_key)
                _salvar_e_recarregar("Senha atualizada!")
    if st.button("Sair"):
        logout()
        st.rerun()
    st.stop()


# ==============================================================================
# APLICAÇÃO PRINCIPAL
# ==============================================================================
def main():
    if "db" not in st.session_state:
        st.session_state.db = carregar_banco()

    _render_flash()

    if not st.session_state.get("autenticado"):
        tela_login()

    ult = st.session_state.get("ultimo_acesso")
    if ult and datetime.now() - ult > timedelta(minutes=TEMPO_SESSAO_MIN):
        logout()
        st.session_state.db = carregar_banco()
        st.warning("Sessão expirada. Faça login novamente.")
        tela_login()
    st.session_state.ultimo_acesso = datetime.now()

    chave = st.session_state.get("usuario_key")
    usuario = DB()["usuarios"].get(chave)
    if not usuario:
        logout()
        st.rerun()
    if usuario.get("trocar_senha"):
        tela_troca_senha(chave)

    try:
        perfil = Perfil(usuario["perfil"].lower())
    except ValueError:
        logout()
        st.error("Perfil inválido.")
        st.stop()

    with st.sidebar:
        exibir_logo_institucional(160)
        st.markdown(f"**{esc(usuario['nome'])}**  \n`{perfil.value.upper()}`")
        if st.button("🚪 Sair (Logout)"):
            auditar("logout", chave)
            try:
                salvar_banco()
            except Exception:
                pass
            logout()
            st.rerun()
        st.divider()

        alunos = DB()["alunos"]
        aluno = None
        aid = None

        if perfil in (Perfil.ALUNO, Perfil.FAMILIA):
            aid = usuario.get("aluno_vinculado")
            st.markdown(f"**Aluno vinculado:** `{esc(aid or '')}`")
            aluno = alunos.get(aid) if aid else None
        elif alunos:
            st.markdown("### 🎓 Seleção do Estudante")

            modalidade_filtro = st.selectbox(
                "1ª Etapa: Modalidade de Ensino",
                MODALIDADES_VALIDAS,
                key="sb_modalidade_filtro"
            )

            alunos_da_modalidade = [
                k for k, v in alunos.items()
                if modalidade_filtro in v["dados"].get("modalidades", [v["dados"].get("modalidade")])
            ]
            alunos_da_modalidade.sort(key=lambda x: alunos[x]["dados"]["nome"])

            if alunos_da_modalidade:
                aid = st.selectbox(
                    "2ª Etapa: Escolha o Estudante",
                    alunos_da_modalidade,
                    format_func=lambda x: f"{alunos[x]['dados']['nome']} ({x})",
                    key="sb_aluno_filtro"
                )
                aluno = alunos.get(aid)
            else:
                st.warning(f"Nenhum estudante na modalidade '{modalidade_filtro}'.")
        else:
            st.warning("Nenhum estudante cadastrado.")

        if aluno and pode(perfil, "gerenciar_matriz"):
            op = aluno["operacao"]
            saldo = op["horas_contratadas"] - op["horas_realizadas"]
            if saldo <= 3.0:
                st.error(f"🚨 **Saldo Crítico:** {saldo:.1f}h. Disparar renovação.")
        if aluno and perfil in (Perfil.ALUNO, Perfil.FAMILIA):
            n = contar_nao_lidas(aid)
            if n:
                st.info(f"🔔 {n} notificação(ões) não lida(s)")
        st.divider()

        if perfil in (Perfil.COORDENADOR, Perfil.ADMIN):
            st.markdown("### 🧭 Navegação")
            modulo_selecionado = st.selectbox("Módulo Principal:", list(MODULOS_ADMIN.keys()))
            st.divider()
            itens_possiveis = [
                m for m in MODULOS_ADMIN[modulo_selecionado]
                if m in VIEWS and pode(perfil, VIEWS[m][1])
            ]
            if not itens_possiveis:
                st.info("Nenhuma ação disponível para o seu perfil neste módulo.")
                st.stop()
            escolha = st.radio("Selecione a Ação:", itens_possiveis)

        elif perfil == Perfil.PROFESSOR:
            st.markdown("### 🧭 Navegação")
            modulo_selecionado = st.selectbox("Módulo Principal:", list(MODULOS_PROFESSOR.keys()))
            st.divider()
            itens_possiveis = [
                m for m in MODULOS_PROFESSOR[modulo_selecionado]
                if m in VIEWS and pode(perfil, VIEWS[m][1])
            ]
            if not itens_possiveis:
                st.info("Nenhuma ação disponível para o seu perfil neste módulo.")
                st.stop()
            escolha = st.radio("Selecione a Ação:", itens_possiveis)

        else:
            itens = [m for m in MENU_SIMPLES[perfil] if m in VIEWS and pode(perfil, VIEWS[m][1])]
            escolha = st.radio("Menu", itens)

    funcao, acao, precisa_aluno = VIEWS[escolha]
    exigir(perfil, acao)
    if precisa_aluno and not aluno:
        st.info("Selecione ou cadastre um estudante para continuar.")
        st.stop()
    funcao(aluno, perfil, usuario)


main()
