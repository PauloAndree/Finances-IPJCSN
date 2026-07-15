from flask import Flask, render_template, request, redirect, url_for, session, flash, jsonify, make_response
import csv
import io
import os
import requests
from flask_sqlalchemy import SQLAlchemy
from flask_bcrypt import Bcrypt
from datetime import datetime, date, timedelta
from sqlalchemy import func
from collections import defaultdict # Novo: Para agregação de dados no relatório
from functools import wraps
import json
import re
import secrets

# ==============================
# CONFIGURAÇÃO DO FLASK
# ==============================
app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY')
if not app.secret_key:
    app.secret_key = os.urandom(24).hex()
    print("AVISO: variável de ambiente SECRET_KEY não definida. Usando uma chave temporária "
          "gerada aleatoriamente — todas as sessões serão invalidadas ao reiniciar o servidor. "
          "Defina SECRET_KEY no ambiente para persistir sessões entre reinícios.")
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///igreja_finance.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db = SQLAlchemy(app)
bcrypt = Bcrypt(app)

# Variáveis globais de API (Mantidas, mas não usadas nesta rota)
# Variáveis globais de API (lê da variável de ambiente `GEMINI_API_KEY`)
# Preferível: configure a chave no sistema e NÃO coloque a chave diretamente no código.
API_KEY = os.environ.get('GEMINI_API_KEY', '')
# Modelo padrão (pode ajustar via variável de ambiente `GEMINI_MODEL`)
GEMINI_MODEL = os.environ.get('GEMINI_MODEL', 'gemini-2.5-flash')
GEMINI_ENDPOINT_TEMPLATE = 'https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}'


def call_gemini(prompt_text, system_instruction=None, model=None, temperature=0.2, max_output_tokens=1024, timeout=30):
    """Chama a API Generative Language (Gemini) do Google de forma server-side.

    Retorna (json_bruto, texto_extraido).
    """
    key = API_KEY
    if not key:
        raise RuntimeError('GEMINI_API_KEY não está definida. Configure a variável de ambiente GEMINI_API_KEY.')

    model = model or GEMINI_MODEL
    url = GEMINI_ENDPOINT_TEMPLATE.format(model=model, key=key)

    payload = {
        "contents": [{"parts": [{"text": prompt_text}]}],
        "generationConfig": {
            "temperature": temperature,
            "maxOutputTokens": max_output_tokens
        }
    }
    if system_instruction:
        payload["systemInstruction"] = {"parts": [{"text": system_instruction}]}

    try:
        resp = requests.post(url, json=payload, timeout=timeout)
        resp.raise_for_status()
        data = resp.json()

        text_result = None
        candidates = data.get('candidates') if isinstance(data, dict) else None
        if candidates:
            parts = candidates[0].get('content', {}).get('parts', [])
            text_result = ' '.join(p.get('text', '') for p in parts if p.get('text')) or None

        return data, text_result

    except requests.HTTPError as e:
        raise RuntimeError(f'Erro HTTP ao chamar Gemini: {e} - resposta: {getattr(e.response, "text", "")}')
    except requests.RequestException as e:
        raise RuntimeError(f'Erro de requisição ao chamar Gemini: {e}')


def format_category_summary(category_dict):
    """Formata um dicionário de categorias -> valores em uma string legível para o prompt."""
    if not category_dict:
        return "Nenhuma transação encontrada nesta categoria."
    return " | ".join(f"[{k}: {v}]" for k, v in category_dict.items())


def build_chat_system_prompt(context, original_query):
    """Monta a instrução de sistema enviada ao Gemini com o contexto financeiro do período."""
    entradas_detalhadas = format_category_summary(context.get('resumo_entradas_por_categoria'))
    saidas_detalhadas = format_category_summary(context.get('resumo_saidas_por_categoria'))

    contexto = f"""
Mês de Referência: {context.get('mes_referencia')}
Data da Consulta: {context.get('data_de_hoje')}
Total Entradas do Mês: {context.get('total_entradas')}
Total Saídas do Mês: {context.get('total_saidas')}
Saldo Atual do Mês: {context.get('saldo_atual')}

Detalhe Entradas por Categoria (Mês): {entradas_detalhadas}
Detalhe Saídas por Categoria (Mês): {saidas_detalhadas}

MOVIMENTAÇÕES DETALHADAS DO MÊS ({context.get('mes_referencia')}):
{json.dumps(context.get('movimentacoes_do_mes_detalhado'), indent=2, ensure_ascii=False)}
"""
    return f"""
Você é um Assistente Financeiro de IA, cordial e profissional.
Sua principal tarefa é responder às perguntas do usuário usando estritamente o CONTEXTO FINANCEIRO e a lista de MOVIMENTAÇÕES DETALHADAS fornecida abaixo.

**INSTRUÇÕES DE FILTRAGEM CRÍTICAS PARA PERGUNTAS COM DATA (Muito Importante):**
1. **Análise Diária:** Se a pergunta do usuário ({original_query}) solicitar um valor para um dia específico (DD/MM/YYYY), você DEVE analisar a lista JSON em 'MOVIMENTAÇÕES DETALHADAS'.
2. **Cálculo:** Filtre a lista JSON para encontrar todas as entradas ou saídas onde:
   * O campo 'data_completa' é IGUAL à data solicitada pelo usuário (Ex: '09/10/2025').
   * O campo 'categoria' é IGUAL à categoria solicitada (Ex: 'Oferta', 'Dízimo', 'Salários/Pagamentos').
   * Some os valores de 'valor' correspondentes.
3. **Prioridade:** Use os dados diários se a data for solicitada. Se a data não for solicitada, use os totais mensais.
4. **Atenção à Categoria:** Sempre responda sobre a CATEGORIA CORRETA solicitada na pergunta (Oferta, Dízimo, etc.).

REGRAS DE RESPOSTA:
1. Responda de forma concisa e clara.
2. Use a moeda Brasileira (R$).
3. Se a data solicitada for fora do 'Mês de Referência' que está no contexto, a resposta deve ser "A data solicitada está fora do contexto de {context.get('mes_referencia')}".
4. Não invente dados. Se não houver valores para a data/categoria solicitada, diga "Nenhuma [Entrada/Saída] de [Categoria] foi encontrada no dia [Data]".

CONTEXTO FINANCEIRO (DADOS EM TEMPO REAL):
{contexto}
"""

# ==============================
# MODELOS DO BANCO DE DADOS
# ==============================
class Usuario(db.Model):
    __tablename__ = "usuarios"
    id_usuario = db.Column(db.Integer, primary_key=True)
    nome = db.Column(db.String(100))
    email = db.Column(db.String(100), unique=True)
    senha = db.Column(db.String(200))
    cargo = db.Column(db.String(20))
    data_criacao = db.Column(db.DateTime, default=datetime.now)
    ativo = db.Column(db.Boolean, default=True)


class Entrada(db.Model):
    __tablename__ = "entradas"
    id_entrada = db.Column(db.Integer, primary_key=True)
    tipo = db.Column(db.String(50)) # Corresponde à categoria (Dízimo, Oferta, etc.)
    forma_pagamento = db.Column(db.String(50))
    valor = db.Column(db.Float)
    data = db.Column(db.DateTime, default=datetime.now)
    descricao = db.Column(db.String(200))
    conta_id = db.Column(db.Integer, db.ForeignKey('contas.id_conta'))
    membro_id = db.Column(db.Integer)  # vínculo com dizimista/membro, adicionado na Fase 3


class Saida(db.Model):
    __tablename__ = "saidas"
    id_saida = db.Column(db.Integer, primary_key=True)
    tipo = db.Column(db.String(50)) # Corresponde à categoria (Contas Fixas, Salários, etc.)
    forma_pagamento = db.Column(db.String(50))
    valor = db.Column(db.Float)
    data = db.Column(db.DateTime, default=datetime.now)
    data_pagamento = db.Column(db.DateTime)
    data_vencimento = db.Column(db.DateTime)
    descricao = db.Column(db.String(200))
    conta_id = db.Column(db.Integer, db.ForeignKey('contas.id_conta'))


class Conta(db.Model):
    __tablename__ = "contas"
    id_conta = db.Column(db.Integer, primary_key=True)
    nome = db.Column(db.String(50), nullable=False)
    tipo = db.Column(db.String(10))  # 'caixa' | 'banco'
    saldo_inicial = db.Column(db.Float, default=0.0)
    ativa = db.Column(db.Boolean, default=True)


class Transferencia(db.Model):
    __tablename__ = "transferencias"
    id_transferencia = db.Column(db.Integer, primary_key=True)
    conta_origem_id = db.Column(db.Integer, db.ForeignKey('contas.id_conta'), nullable=False)
    conta_destino_id = db.Column(db.Integer, db.ForeignKey('contas.id_conta'), nullable=False)
    valor = db.Column(db.Float, nullable=False)
    data = db.Column(db.DateTime, default=datetime.now)
    descricao = db.Column(db.String(200))
    usuario = db.Column(db.String(100))
    conta_origem = db.relationship('Conta', foreign_keys=[conta_origem_id])
    conta_destino = db.relationship('Conta', foreign_keys=[conta_destino_id])


class Membro(db.Model):
    __tablename__ = "membros"
    id_membro = db.Column(db.Integer, primary_key=True)
    nome = db.Column(db.String(100), nullable=False)
    cpf = db.Column(db.String(14))
    email = db.Column(db.String(100))
    telefone = db.Column(db.String(20))
    ativo = db.Column(db.Boolean, default=True)
    data_criacao = db.Column(db.DateTime, default=datetime.now)


class Orcamento(db.Model):
    __tablename__ = "orcamentos"
    id_orcamento = db.Column(db.Integer, primary_key=True)
    ano = db.Column(db.Integer, nullable=False)
    mes = db.Column(db.Integer, nullable=False)
    categoria_codigo = db.Column(db.String(20), nullable=False)
    tipo_categoria = db.Column(db.String(10), nullable=False)  # 'RECEITA' | 'SAIDA'
    valor_previsto = db.Column(db.Float, nullable=False, default=0.0)
    __table_args__ = (db.UniqueConstraint('ano', 'mes', 'categoria_codigo', 'tipo_categoria', name='uq_orcamento_periodo'),)


class HistoricoAlteracao(db.Model):
    __tablename__ = "historico_alteracoes"
    id_historico = db.Column(db.Integer, primary_key=True)
    tabela = db.Column(db.String(20))       # 'entradas' | 'saidas'
    id_registro = db.Column(db.Integer)
    operacao = db.Column(db.String(10))     # 'criar' | 'editar' | 'excluir'
    usuario = db.Column(db.String(100))
    data_hora = db.Column(db.DateTime, default=datetime.now)
    dados_antes = db.Column(db.Text)   # snapshot JSON ou NULL
    dados_depois = db.Column(db.Text)  # snapshot JSON ou NULL


# ==============================
# PLANO DE CONTAS (fonte única — usado por todas as rotas e templates)
# ==============================
CONTAS_RECEITA = [
    {"codigo": "1.1", "nome": "Dízimos"},
    {"codigo": "1.2", "nome": "Ofertas"},
    {"codigo": "1.3", "nome": "Contribuições"},
    {"codigo": "1.4", "nome": "Eventos"},
    {"codigo": "1.5", "nome": "Doações Especiais"},
    {"codigo": "1.6", "nome": "Outras Receitas"},
]

CONTAS_SAIDA = [
    {"codigo": "2.1", "nome": "Despesas com Pessoal"},
    {"codigo": "2.2", "nome": "Despesas Operacionais Fixas"},
    {"codigo": "2.3", "nome": "Despesas Administrativas"},
    {"codigo": "2.4", "nome": "Despesas de Manutenção"},
    {"codigo": "2.5", "nome": "Investimentos/Ações Sociais"},
    {"codigo": "Outro", "nome": "Outro"},
]

FORMAS_PAGAMENTO_ENTRADA = ['Dinheiro', 'Pix', 'Débito', 'Crédito', 'Transferência']
FORMAS_PAGAMENTO_SAIDA = ['Dinheiro', 'Pix', 'Débito', 'Crédito', 'Boleto']

NOME_RECEITA_POR_CODIGO = {c['codigo']: c['nome'] for c in CONTAS_RECEITA}
NOME_SAIDA_POR_CODIGO = {c['codigo']: c['nome'] for c in CONTAS_SAIDA}


def categoria_display(codigo, mapa_nomes):
    """Formata 'codigo - nome' para exibição; usa o próprio valor se o código não for reconhecido."""
    codigo = (codigo or '').strip()
    nome = mapa_nomes.get(codigo)
    return f"{codigo} - {nome}" if nome else codigo


# ==============================
# CONTROLE DE ACESSO POR CARGO
# ==============================
CARGOS_FINANCEIRO = ('administrador', 'tesoureiro')  # podem lançar/editar/excluir entradas e saídas
CARGOS_DETALHE = ('administrador', 'tesoureiro', 'secretario', 'pastor')  # veem listagens/relatório/contas a pagar; 'consulta' só vê o resumo do dashboard


def login_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if 'usuario' not in session:
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return wrapper


def roles_required(*cargos_permitidos):
    def decorator(f):
        @wraps(f)
        def wrapper(*args, **kwargs):
            if 'usuario' not in session:
                return redirect(url_for('login'))
            if session.get('cargo') not in cargos_permitidos:
                flash('Você não tem permissão para acessar esta página.', 'erro')
                return redirect(url_for('dashboard'))
            return f(*args, **kwargs)
        return wrapper
    return decorator


def pode_editar_financeiro():
    return session.get('cargo') in CARGOS_FINANCEIRO


def snapshot_registro(obj):
    """Serializa as colunas de um registro do SQLAlchemy em um dict JSON-serializável."""
    dados = {}
    for col in obj.__table__.columns:
        valor = getattr(obj, col.name)
        if isinstance(valor, datetime):
            valor = valor.strftime('%Y-%m-%d %H:%M:%S')
        dados[col.name] = valor
    return dados


def registrar_historico(tabela, id_registro, operacao, antes=None, depois=None):
    """Grava uma linha de auditoria. Deve ser chamado antes do commit da alteração
    correspondente, para que ambos entrem na mesma transação."""
    db.session.add(HistoricoAlteracao(
        tabela=tabela,
        id_registro=id_registro,
        operacao=operacao,
        usuario=session.get('usuario'),
        dados_antes=json.dumps(antes, ensure_ascii=False) if antes is not None else None,
        dados_depois=json.dumps(depois, ensure_ascii=False) if depois is not None else None,
    ))


@app.context_processor
def inject_globais():
    return dict(
        usuario_atual=session.get('usuario'),
        cargo_atual=session.get('cargo'),
        pode_editar=pode_editar_financeiro(),
    )


# ==============================
# FUNÇÕES AUXILIARES DE DADOS E RELATÓRIO
# ==============================

def calcular_saldo_conta(conta):
    """Saldo atual de uma conta: saldo inicial + entradas - saídas pagas +/- transferências."""
    total_entradas = db.session.query(func.sum(Entrada.valor)).filter(Entrada.conta_id == conta.id_conta).scalar() or 0
    total_saidas_pagas = db.session.query(func.sum(Saida.valor)).filter(
        Saida.conta_id == conta.id_conta, Saida.data_pagamento.isnot(None)
    ).scalar() or 0
    total_recebido = db.session.query(func.sum(Transferencia.valor)).filter(
        Transferencia.conta_destino_id == conta.id_conta
    ).scalar() or 0
    total_enviado = db.session.query(func.sum(Transferencia.valor)).filter(
        Transferencia.conta_origem_id == conta.id_conta
    ).scalar() or 0
    return (conta.saldo_inicial or 0) + total_entradas - total_saidas_pagas + total_recebido - total_enviado

def calcular_dados_tendencia():
    """Calcula a evolução mensal de receitas e despesas nos últimos 6 meses."""
    hoje = date.today()
    meses_a_exibir = 6
    
    trend_data = defaultdict(lambda: {"receita": 0.0, "despesa": 0.0})
    labels = []
    month_keys = []
    
    # 1. Determina as chaves e rótulos dos últimos 6 meses (em ordem crescente)
    current_y, current_m = hoje.year, hoje.month
    
    # Calcula a data de início (6 meses atrás)
    for i in range(meses_a_exibir):
        if current_m == 1:
            current_m = 12
            current_y -= 1
        else:
            current_m -= 1
    
    # Agora current_y/current_m é o mês anterior ao primeiro dos 6 meses.
    # Avança para o primeiro dos 6 meses.
    for i in range(meses_a_exibir):
        if current_m == 12:
            current_m = 1
            current_y += 1
        else:
            current_m += 1
            
        key = f"{current_y:04d}-{current_m:02d}"
        month_keys.append(key)
        # Exibe o rótulo como 'MM/AA'
        labels.append(f"{current_m:02d}/{current_y%100:02d}")

    # Pega a data de início (primeiro dia do primeiro mês dos 6)
    start_date_str = month_keys[0] + "-01"
    start_date = datetime.strptime(start_date_str, '%Y-%m-%d').date()

    # 2. Busca dados do banco de dados (apenas a partir da data de início)
    # Note: Usamos func.date() para garantir que a comparação seja feita apenas na parte da data
    entradas_trend = Entrada.query.filter(func.date(Entrada.data) >= start_date).all()
    saidas_trend = Saida.query.filter(func.date(Saida.data) >= start_date).all()
    
    # 3. Agrega dados
    for e in entradas_trend:
        key = e.data.strftime("%Y-%m")
        trend_data[key]["receita"] += e.valor or 0

    for s in saidas_trend:
        key = s.data.strftime("%Y-%m")
        trend_data[key]["despesa"] += s.valor or 0

    # 4. Extrai dados na ordem cronológica correta (usando as chaves pré-definidas)
    receitas = []
    despesas = []
    for key in month_keys:
        receitas.append(trend_data[key]["receita"])
        despesas.append(trend_data[key]["despesa"])

    return {
        "labels": labels,
        "receitas": receitas,
        "despesas": despesas
    }

def get_contas_a_pagar(dias_proximos=7):
    """Retorna as saídas ainda não pagas, já anotadas com categoria_nome e separadas em
    atrasadas (vencimento no passado), próximas (vencendo nos próximos N dias), futuras
    (vencimento além disso) e sem vencimento definido. Toda saída pendente cai em uma
    dessas categorias — nenhuma fica sem aparecer na tela."""
    hoje = date.today()
    limite = hoje + timedelta(days=dias_proximos)

    pendentes = Saida.query.filter(Saida.data_pagamento.is_(None)).order_by(Saida.data_vencimento.asc()).all()
    for s in pendentes:
        setattr(s, 'categoria_nome', categoria_display(s.tipo, NOME_SAIDA_POR_CODIGO))

    atrasadas = [s for s in pendentes if s.data_vencimento and s.data_vencimento.date() < hoje]
    proximas = [s for s in pendentes if s.data_vencimento and hoje <= s.data_vencimento.date() <= limite]
    futuras = [s for s in pendentes if s.data_vencimento and s.data_vencimento.date() > limite]
    sem_vencimento = [s for s in pendentes if not s.data_vencimento]

    return {
        'todas': pendentes,
        'atrasadas': atrasadas,
        'proximas': proximas,
        'futuras': futuras,
        'sem_vencimento': sem_vencimento,
    }


def get_comparativo_orcamento(ano, mes):
    """Compara valores previstos (Orcamento) com o realizado (Entrada/Saida) por categoria no período."""
    mes_str = f"{ano:04d}-{mes:02d}"
    entradas_mes = Entrada.query.filter(func.strftime("%Y-%m", Entrada.data) == mes_str).all()
    saidas_mes = Saida.query.filter(func.strftime("%Y-%m", Saida.data) == mes_str).all()

    realizado_receita = defaultdict(float)
    for e in entradas_mes:
        realizado_receita[(e.tipo or '').strip()] += e.valor or 0

    realizado_saida = defaultdict(float)
    for s in saidas_mes:
        realizado_saida[(s.tipo or '').strip()] += s.valor or 0

    previsto_por_codigo = {
        (o.tipo_categoria, o.categoria_codigo): o.valor_previsto
        for o in Orcamento.query.filter_by(ano=ano, mes=mes).all()
    }

    def montar_linhas(contas, tipo_categoria, realizado_por_codigo):
        linhas = []
        for conta in contas:
            codigo = conta['codigo']
            previsto = previsto_por_codigo.get((tipo_categoria, codigo), 0)
            realizado = realizado_por_codigo.get(codigo, 0)
            linhas.append({
                'codigo': codigo,
                'nome': conta['nome'],
                'previsto': previsto,
                'realizado': realizado,
                'diferenca': realizado - previsto,
            })
        return linhas

    linhas_receita = montar_linhas(CONTAS_RECEITA, 'RECEITA', realizado_receita)
    linhas_saida = montar_linhas(CONTAS_SAIDA, 'SAIDA', realizado_saida)

    return {
        'receitas': linhas_receita,
        'saidas': linhas_saida,
        'total_previsto_receita': sum(l['previsto'] for l in linhas_receita),
        'total_realizado_receita': sum(l['realizado'] for l in linhas_receita),
        'total_previsto_saida': sum(l['previsto'] for l in linhas_saida),
        'total_realizado_saida': sum(l['realizado'] for l in linhas_saida),
    }


# Mantidas as funções get_finance_summary, get_chatbot_context_json e extract_date_from_query
def get_finance_summary():
    """Mantida para o Dashboard (usa o mês atual)."""
    hoje = date.today()
    mes_str = hoje.strftime("%Y-%m")

    entradas_mes = Entrada.query.filter(func.strftime("%Y-%m", Entrada.data) == mes_str).all()
    total_entradas = sum(e.valor or 0 for e in entradas_mes)
    # Usar data_pagamento para considerar apenas saídas pagas no mês de referência
    saidas_mes = Saida.query.filter(Saida.data_pagamento != None).filter(func.strftime("%Y-%m", Saida.data_pagamento) == mes_str).all()
    total_saidas = sum(s.valor or 0 for s in saidas_mes)
    saldo = total_entradas - total_saidas
    entradas_hoje = Entrada.query.filter(func.date(Entrada.data) == hoje).all()
    total_dia = sum(e.valor or 0 for e in entradas_hoje)

    return {
        'total_entradas': f"{total_entradas:.2f}",
        'total_saidas': f"{total_saidas:.2f}",
        'saldo_atual': f"{saldo:.2f}",
        'total_entradas_hoje': f"{total_dia:.2f}",
    }


def get_chatbot_context_json(ref_year, ref_month):
    """
    Busca o resumo financeiro para o ano e mês especificados (ou o mês atual se não fornecidos)
    e formata como string JSON para o LLM.
    """
    # Cria a string de referência YYYY-MM para o SQL
    mes_str = f"{ref_year:04d}-{ref_month:02d}"

    # Entradas do Mês (Listagem completa)
    entradas_mes = Entrada.query.filter(func.strftime("%Y-%m", Entrada.data) == mes_str).all()
    # Saídas do Mês (Listagem completa)
    saidas_mes = Saida.query.filter(func.strftime("%Y-%m", Saida.data) == mes_str).all()

    # Calcular totais
    total_entradas = sum(e.valor or 0 for e in entradas_mes)
    total_saidas = sum(s.valor or 0 for s in saidas_mes)
    saldo = total_entradas - total_saidas
    
    # Resumos por Tipo (Entradas)
    resumo_entradas_tipo = {}
    for e in entradas_mes:
        tipo = categoria_display(e.tipo, NOME_RECEITA_POR_CODIGO) or 'Outro'
        resumo_entradas_tipo[tipo] = resumo_entradas_tipo.get(tipo, 0) + (e.valor or 0)

    # Resumos por Tipo (Saídas)
    resumo_saidas_tipo = {}
    for s in saidas_mes:
        tipo = categoria_display(s.tipo, NOME_SAIDA_POR_CODIGO) or 'Outro'
        resumo_saidas_tipo[tipo] = resumo_saidas_tipo.get(tipo, 0) + (s.valor or 0)

    # Adiciona lista de movimentações detalhada
    lista_movimentacoes_detalhada = []
    for e in entradas_mes:
        lista_movimentacoes_detalhada.append({
            "tipo": "Entrada",
            "categoria": categoria_display(e.tipo, NOME_RECEITA_POR_CODIGO),
            "valor": f"R$ {e.valor:.2f}",
            "data_completa": e.data.strftime('%d/%m/%Y'),
            "descricao": e.descricao
        })
    for s in saidas_mes:
        lista_movimentacoes_detalhada.append({
            "tipo": "Saida",
            "categoria": categoria_display(s.tipo, NOME_SAIDA_POR_CODIGO),
            "valor": f"R$ {s.valor:.2f}",
            "data_completa": s.data.strftime('%d/%m/%Y'),
            "descricao": s.descricao
        })


    data_context = {
        "mes_referencia": datetime(ref_year, ref_month, 1).strftime("%B de %Y").capitalize(),
        "total_entradas": f"R$ {total_entradas:.2f}",
        "total_saidas": f"R$ {total_saidas:.2f}",
        "saldo_atual": f"R$ {saldo:.2f}",
        "resumo_entradas_por_categoria": {k: f"R$ {v:.2f}" for k, v in resumo_entradas_tipo.items()},
        "resumo_saidas_por_categoria": {k: f"R$ {v:.2f}" for k, v in resumo_saidas_tipo.items()},
        "data_de_hoje": datetime.now().strftime('%d/%m/%Y'),
        "movimentacoes_do_mes_detalhado": lista_movimentacoes_detalhada
    }

    return json.dumps(data_context, indent=2, ensure_ascii=False)


def extract_date_from_query(query):
    """
    Tenta extrair uma data (DD/MM/YYYY ou DD-MM-YYYY) da string de consulta.
    Retorna (year, month) ou None.
    """
    # Regex para buscar DD/MM/YYYY, DD-MM-YYYY, D/M/YYYY etc.
    match = re.search(r'(\d{1,2})[/-](\d{1,2})[/-](\d{4})', query)
    if match:
        try:
            # Note que a ordem é DD, MM, YYYY
            day = int(match.group(1))
            month = int(match.group(2))
            year = int(match.group(3))
            
            # Apenas para validar que é uma data razoável antes de retornar
            datetime(year, month, day) 
            return year, month # Retorna apenas o mês e o ano para o contexto
        except ValueError:
            # Data inválida (ex: 30/02/2024)
            return None
    return None

# ==============================
# ROTAS DO SISTEMA (ATUALIZADA: /relatorio)
# ==============================

@app.route('/')
def index():
    return redirect(url_for('login'))

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        email = request.form['email']
        senha = request.form['senha']
        usuario = Usuario.query.filter_by(email=email).first()
        if usuario and not usuario.ativo:
            flash('Este usuário foi desativado.', 'erro')
            return redirect(url_for('login'))
        if usuario and bcrypt.check_password_hash(usuario.senha, senha):
            session['usuario'] = usuario.nome
            session['cargo'] = usuario.cargo
            session['usuario_id'] = usuario.id_usuario
            return redirect(url_for('dashboard'))
        else:
            flash('Email ou senha incorretos!', 'erro')
            return redirect(url_for('login'))
    return render_template('login.html')

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))

@app.route('/dashboard')
@login_required
def dashboard():
    summary = get_finance_summary()
    hoje = date.today()

    # Cargo 'consulta': só o resumo básico, sem widgets, chat ou ações — nada de detalhe.
    if session.get('cargo') == 'consulta':
        return render_template(
            'dashboard.html',
            hoje=hoje,
            total_entradas=float(summary['total_entradas']),
            total_saidas=float(summary['total_saidas']),
            saldo=float(summary['saldo_atual']),
            total_dia=float(summary['total_entradas_hoje']),
            modo_consulta=True,
        )

    contas_a_pagar = get_contas_a_pagar()
    contas_destaque = (contas_a_pagar['atrasadas'] + contas_a_pagar['proximas'])[:5]
    orcamento_mes = get_comparativo_orcamento(hoje.year, hoje.month)

    return render_template(
        'dashboard.html',
        hoje=hoje,
        total_entradas=float(summary['total_entradas']),
        total_saidas=float(summary['total_saidas']),
        saldo=float(summary['saldo_atual']),
        total_dia=float(summary['total_entradas_hoje']),
        contas_destaque=contas_destaque,
        total_atrasadas=len(contas_a_pagar['atrasadas']),
        total_proximas=len(contas_a_pagar['proximas']),
        orcamento_previsto_receita=orcamento_mes['total_previsto_receita'],
        orcamento_realizado_receita=orcamento_mes['total_realizado_receita'],
        orcamento_previsto_saida=orcamento_mes['total_previsto_saida'],
        orcamento_realizado_saida=orcamento_mes['total_realizado_saida'],
    )

@app.route('/entradas', methods=['GET', 'POST'])
@roles_required(*CARGOS_DETALHE)
def entradas():
    if request.method == 'POST':
        if not pode_editar_financeiro():
            flash('Você não tem permissão para registrar lançamentos.', 'erro')
            return redirect(url_for('entradas'))

        tipo = request.form['categoria_id']
        forma_pagamento = request.form['forma_pagamento']
        try:
            valor = float(request.form['valor'])
        except ValueError:
            flash('Valor inválido.', 'erro')
            return redirect(url_for('entradas'))

        descricao = request.form['descricao']
        data_entrada = datetime.now()

        if 'data' in request.form and request.form['data']:
            try:
                data_entrada = datetime.strptime(request.form['data'], '%Y-%m-%d')
            except ValueError:
                flash('Formato de data inválido, usando data atual.', 'aviso')

        conta_id = request.form.get('conta_id', type=int)
        membro_id = request.form.get('membro_id', type=int)

        nova_entrada = Entrada(
            tipo=tipo,
            forma_pagamento=forma_pagamento,
            valor=valor,
            descricao=descricao,
            data=data_entrada,
            conta_id=conta_id,
            membro_id=membro_id,
        )
        db.session.add(nova_entrada)
        db.session.flush()
        registrar_historico('entradas', nova_entrada.id_entrada, 'criar', depois=snapshot_registro(nova_entrada))
        db.session.commit()
        flash('Entrada registrada com sucesso!', 'sucesso')
        return redirect(url_for('entradas'))

    # 🔹 Carrega todas as entradas
    todas_entradas = Entrada.query.order_by(Entrada.data.desc()).all()
    contas_por_id = {c.id_conta: c.nome for c in Conta.query.all()}
    membros_por_id = {m.id_membro: m.nome for m in Membro.query.all()}

    # Anexa atributos temporários aos objetos de entrada para uso no template
    for e in todas_entradas:
        codigo = (e.tipo or '').strip()
        setattr(e, 'categoria_codigo', codigo)
        setattr(e, 'categoria_nome', NOME_RECEITA_POR_CODIGO.get(codigo, codigo))
        setattr(e, 'conta_nome', contas_por_id.get(e.conta_id, '-'))
        setattr(e, 'membro_nome', membros_por_id.get(e.membro_id, '-'))

    return render_template(
        'entradas.html',
        entradas=todas_entradas,
        contas_receita=CONTAS_RECEITA,
        formas_pagamento=FORMAS_PAGAMENTO_ENTRADA,
        contas=Conta.query.filter_by(ativa=True).order_by(Conta.nome).all(),
        membros=Membro.query.filter_by(ativo=True).order_by(Membro.nome).all(),
    )


@app.route('/saidas', methods=['GET', 'POST'])
@roles_required(*CARGOS_DETALHE)
def saidas():
    if request.method == 'POST':
        if not pode_editar_financeiro():
            flash('Você não tem permissão para registrar lançamentos.', 'erro')
            return redirect(url_for('saidas'))

        # Aceita tanto 'tipo' (template atualizado) quanto 'categoria_id' (legado)
        tipo = request.form.get('tipo') or request.form.get('categoria_id') or 'Outro'
        forma_pagamento = request.form.get('forma_pagamento', '')

        # Tratamento seguro do valor para evitar exceções
        valor_str = request.form.get('valor', '')
        try:
            valor = float(valor_str)
        except (ValueError, TypeError):
            flash('Valor inválido.', 'erro')
            return redirect(url_for('saidas'))

        descricao = request.form.get('descricao', '')
        data_saida = datetime.now()
        if 'data' in request.form and request.form['data']:
            try:
                data_saida = datetime.strptime(request.form['data'], '%Y-%m-%d')
            except ValueError:
                flash('Formato de data inválido, usando data atual.', 'aviso')

        # Capturar datas de pagamento e vencimento
        data_pagamento = None
        if 'data_pagamento' in request.form and request.form['data_pagamento']:
            try:
                data_pagamento = datetime.strptime(request.form['data_pagamento'], '%Y-%m-%d')
            except ValueError:
                flash('Formato de data de pagamento inválido.', 'aviso')

        data_vencimento = None
        if 'data_vencimento' in request.form and request.form['data_vencimento']:
            try:
                data_vencimento = datetime.strptime(request.form['data_vencimento'], '%Y-%m-%d')
            except ValueError:
                flash('Formato de data de vencimento inválido.', 'aviso')

        conta_id = request.form.get('conta_id', type=int)

        nova_saida = Saida(tipo=tipo, forma_pagamento=forma_pagamento, valor=valor, descricao=descricao, data=data_saida, data_pagamento=data_pagamento, data_vencimento=data_vencimento, conta_id=conta_id)
        db.session.add(nova_saida)
        db.session.flush()
        registrar_historico('saidas', nova_saida.id_saida, 'criar', depois=snapshot_registro(nova_saida))
        db.session.commit()
        flash('Saída registrada com sucesso!', 'sucesso')
        return redirect(url_for('saidas'))
    todas_saidas = Saida.query.order_by(Saida.data.desc()).all()
    contas_por_id = {c.id_conta: c.nome for c in Conta.query.all()}
    for s in todas_saidas:
        setattr(s, 'categoria_nome', categoria_display(s.tipo, NOME_SAIDA_POR_CODIGO))
        setattr(s, 'conta_nome', contas_por_id.get(s.conta_id, '-'))

    return render_template(
        'saidas.html',
        saidas=todas_saidas,
        contas_saida=CONTAS_SAIDA,
        formas_pagamento=FORMAS_PAGAMENTO_SAIDA,
        contas=Conta.query.filter_by(ativa=True).order_by(Conta.nome).all(),
    )

# ==============================
# ROTAS DE EDIÇÃO E EXCLUSÃO
# ==============================

@app.route('/editar-entrada/<int:id>', methods=['GET', 'POST'])
@roles_required(*CARGOS_FINANCEIRO)
def editar_entrada(id):
    entrada = Entrada.query.get_or_404(id)

    if request.method == 'POST':
        antes = snapshot_registro(entrada)
        entrada.tipo = request.form.get('categoria_id') or entrada.tipo
        entrada.forma_pagamento = request.form.get('forma_pagamento', entrada.forma_pagamento)
        
        try:
            entrada.valor = float(request.form.get('valor', entrada.valor))
        except (ValueError, TypeError):
            flash('Valor inválido.', 'erro')
            return redirect(url_for('editar_entrada', id=id))
        
        entrada.descricao = request.form.get('descricao', '')
        entrada.conta_id = request.form.get('conta_id', type=int) or entrada.conta_id
        entrada.membro_id = request.form.get('membro_id', type=int)

        if request.form.get('data'):
            try:
                entrada.data = datetime.strptime(request.form['data'], '%Y-%m-%d')
            except ValueError:
                flash('Formato de data inválido.', 'aviso')

        registrar_historico('entradas', entrada.id_entrada, 'editar', antes=antes, depois=snapshot_registro(entrada))
        db.session.commit()
        flash('Entrada atualizada com sucesso!', 'sucesso')
        return redirect(url_for('entradas'))

    return render_template(
        'editar_entrada.html',
        entrada=entrada,
        contas_receita=CONTAS_RECEITA,
        formas_pagamento=FORMAS_PAGAMENTO_ENTRADA,
        contas=Conta.query.filter_by(ativa=True).order_by(Conta.nome).all(),
        membros=Membro.query.filter_by(ativo=True).order_by(Membro.nome).all(),
    )

@app.route('/excluir-entrada/<int:id>', methods=['POST'])
@roles_required(*CARGOS_FINANCEIRO)
def excluir_entrada(id):
    entrada = Entrada.query.get_or_404(id)
    registrar_historico('entradas', entrada.id_entrada, 'excluir', antes=snapshot_registro(entrada))
    db.session.delete(entrada)
    db.session.commit()
    flash('Entrada excluída com sucesso!', 'sucesso')
    return redirect(url_for('entradas'))

@app.route('/editar-saida/<int:id>', methods=['GET', 'POST'])
@roles_required(*CARGOS_FINANCEIRO)
def editar_saida(id):
    saida = Saida.query.get_or_404(id)
    
    if request.method == 'POST':
        antes = snapshot_registro(saida)
        saida.tipo = request.form.get('tipo', saida.tipo)
        saida.forma_pagamento = request.form.get('forma_pagamento', saida.forma_pagamento)
        
        try:
            saida.valor = float(request.form.get('valor', saida.valor))
        except (ValueError, TypeError):
            flash('Valor inválido.', 'erro')
            return redirect(url_for('editar_saida', id=id))
        
        saida.descricao = request.form.get('descricao', '')
        
        if request.form.get('data'):
            try:
                saida.data = datetime.strptime(request.form['data'], '%Y-%m-%d')
            except ValueError:
                flash('Formato de data inválido.', 'aviso')
        
        # Capturar datas de pagamento e vencimento
        if request.form.get('data_pagamento'):
            try:
                saida.data_pagamento = datetime.strptime(request.form['data_pagamento'], '%Y-%m-%d')
            except ValueError:
                flash('Formato de data de pagamento inválido.', 'aviso')
        else:
            saida.data_pagamento = None
        
        if request.form.get('data_vencimento'):
            try:
                saida.data_vencimento = datetime.strptime(request.form['data_vencimento'], '%Y-%m-%d')
            except ValueError:
                flash('Formato de data de vencimento inválido.', 'aviso')
        else:
            saida.data_vencimento = None

        saida.conta_id = request.form.get('conta_id', type=int) or saida.conta_id

        registrar_historico('saidas', saida.id_saida, 'editar', antes=antes, depois=snapshot_registro(saida))
        db.session.commit()
        flash('Saída atualizada com sucesso!', 'sucesso')
        return redirect(url_for('saidas'))

    return render_template(
        'editar_saida.html',
        saida=saida,
        contas_saida=CONTAS_SAIDA,
        formas_pagamento=FORMAS_PAGAMENTO_SAIDA,
        contas=Conta.query.filter_by(ativa=True).order_by(Conta.nome).all(),
    )

@app.route('/excluir-saida/<int:id>', methods=['POST'])
@roles_required(*CARGOS_FINANCEIRO)
def excluir_saida(id):
    saida = Saida.query.get_or_404(id)
    registrar_historico('saidas', saida.id_saida, 'excluir', antes=snapshot_registro(saida))
    db.session.delete(saida)
    db.session.commit()
    flash('Saída excluída com sucesso!', 'sucesso')
    return redirect(url_for('saidas'))

@app.route('/contas-a-pagar', methods=['GET'])
@roles_required(*CARGOS_DETALHE)
def contas_a_pagar():
    contas = get_contas_a_pagar()
    return render_template(
        'contas_a_pagar.html',
        atrasadas=contas['atrasadas'],
        proximas=contas['proximas'],
        futuras=contas['futuras'],
        sem_vencimento=contas['sem_vencimento'],
    )


@app.route('/saidas/<int:id>/marcar-pago', methods=['POST'])
@roles_required(*CARGOS_FINANCEIRO)
def marcar_saida_paga(id):
    saida = Saida.query.get_or_404(id)
    antes = snapshot_registro(saida)
    saida.data_pagamento = datetime.now()
    registrar_historico('saidas', saida.id_saida, 'editar', antes=antes, depois=snapshot_registro(saida))
    db.session.commit()
    flash('Saída marcada como paga!', 'sucesso')
    return redirect(request.referrer or url_for('contas_a_pagar'))


@app.route('/contas', methods=['GET', 'POST'])
@roles_required(*CARGOS_FINANCEIRO)
def contas():
    if request.method == 'POST':
        nome = request.form.get('nome', '').strip()
        tipo = request.form.get('tipo', 'caixa')
        try:
            saldo_inicial = float(request.form.get('saldo_inicial', '0') or 0)
        except ValueError:
            flash('Saldo inicial inválido.', 'erro')
            return redirect(url_for('contas'))

        if not nome:
            flash('Nome da conta é obrigatório.', 'erro')
            return redirect(url_for('contas'))

        db.session.add(Conta(nome=nome, tipo=tipo, saldo_inicial=saldo_inicial, ativa=True))
        db.session.commit()
        flash('Conta criada com sucesso!', 'sucesso')
        return redirect(url_for('contas'))

    todas_contas = Conta.query.filter_by(ativa=True).order_by(Conta.nome).all()
    contas_com_saldo = [(c, calcular_saldo_conta(c)) for c in todas_contas]
    return render_template('contas.html', contas_com_saldo=contas_com_saldo)


@app.route('/editar-conta/<int:id>', methods=['GET', 'POST'])
@roles_required(*CARGOS_FINANCEIRO)
def editar_conta(id):
    conta = Conta.query.get_or_404(id)

    if request.method == 'POST':
        conta.nome = request.form.get('nome', conta.nome).strip() or conta.nome
        conta.tipo = request.form.get('tipo', conta.tipo)
        try:
            conta.saldo_inicial = float(request.form.get('saldo_inicial', conta.saldo_inicial))
        except (ValueError, TypeError):
            flash('Saldo inicial inválido.', 'erro')
            return redirect(url_for('editar_conta', id=id))

        db.session.commit()
        flash('Conta atualizada com sucesso!', 'sucesso')
        return redirect(url_for('contas'))

    return render_template('editar_conta.html', conta=conta)


@app.route('/excluir-conta/<int:id>', methods=['POST'])
@roles_required(*CARGOS_FINANCEIRO)
def excluir_conta(id):
    conta = Conta.query.get_or_404(id)
    if Conta.query.filter_by(ativa=True).count() <= 1:
        flash('Não é possível excluir a única conta ativa.', 'erro')
        return redirect(url_for('contas'))

    conta.ativa = False
    db.session.commit()
    flash('Conta removida com sucesso!', 'sucesso')
    return redirect(url_for('contas'))


@app.route('/transferencias', methods=['GET', 'POST'])
@roles_required(*CARGOS_FINANCEIRO)
def transferencias():
    if request.method == 'POST':
        try:
            conta_origem_id = int(request.form['conta_origem_id'])
            conta_destino_id = int(request.form['conta_destino_id'])
            valor = float(request.form['valor'])
        except (ValueError, KeyError):
            flash('Dados inválidos para a transferência.', 'erro')
            return redirect(url_for('transferencias'))

        if conta_origem_id == conta_destino_id:
            flash('A conta de origem e destino não podem ser a mesma.', 'erro')
            return redirect(url_for('transferencias'))

        nova_transferencia = Transferencia(
            conta_origem_id=conta_origem_id,
            conta_destino_id=conta_destino_id,
            valor=valor,
            descricao=request.form.get('descricao', ''),
            usuario=session.get('usuario'),
        )
        db.session.add(nova_transferencia)
        db.session.flush()

        conta_origem = Conta.query.get(conta_origem_id)
        conta_destino = Conta.query.get(conta_destino_id)
        registrar_historico('transferencias', nova_transferencia.id_transferencia, 'criar', depois={
            'conta_origem': conta_origem.nome if conta_origem else str(conta_origem_id),
            'conta_destino': conta_destino.nome if conta_destino else str(conta_destino_id),
            'valor': valor,
            'descricao': nova_transferencia.descricao,
        })
        db.session.commit()
        flash('Transferência registrada com sucesso!', 'sucesso')
        return redirect(url_for('transferencias'))

    todas_contas = Conta.query.filter_by(ativa=True).order_by(Conta.nome).all()
    historico = Transferencia.query.order_by(Transferencia.data.desc()).all()
    return render_template('transferencias.html', contas=todas_contas, historico=historico)


@app.route('/membros', methods=['GET', 'POST'])
@roles_required(*CARGOS_FINANCEIRO)
def membros():
    if request.method == 'POST':
        nome = request.form.get('nome', '').strip()
        if not nome:
            flash('Nome do membro é obrigatório.', 'erro')
            return redirect(url_for('membros'))

        novo_membro = Membro(
            nome=nome,
            cpf=request.form.get('cpf', '').strip(),
            email=request.form.get('email', '').strip(),
            telefone=request.form.get('telefone', '').strip(),
        )
        db.session.add(novo_membro)
        db.session.commit()
        flash('Membro cadastrado com sucesso!', 'sucesso')
        return redirect(url_for('membros'))

    todos_membros = Membro.query.filter_by(ativo=True).order_by(Membro.nome).all()
    return render_template('membros.html', membros=todos_membros)


@app.route('/editar-membro/<int:id>', methods=['GET', 'POST'])
@roles_required(*CARGOS_FINANCEIRO)
def editar_membro(id):
    membro = Membro.query.get_or_404(id)

    if request.method == 'POST':
        membro.nome = request.form.get('nome', membro.nome).strip() or membro.nome
        membro.cpf = request.form.get('cpf', '').strip()
        membro.email = request.form.get('email', '').strip()
        membro.telefone = request.form.get('telefone', '').strip()
        db.session.commit()
        flash('Membro atualizado com sucesso!', 'sucesso')
        return redirect(url_for('membros'))

    return render_template('editar_membro.html', membro=membro)


@app.route('/excluir-membro/<int:id>', methods=['POST'])
@roles_required(*CARGOS_FINANCEIRO)
def excluir_membro(id):
    membro = Membro.query.get_or_404(id)
    membro.ativo = False
    db.session.commit()
    flash('Membro removido com sucesso!', 'sucesso')
    return redirect(url_for('membros'))


@app.route('/membros/<int:id>/recibo', methods=['GET'])
@roles_required(*CARGOS_FINANCEIRO)
def recibo_membro(id):
    membro = Membro.query.get_or_404(id)
    ano = request.args.get('ano', type=int) or date.today().year

    entradas_membro = Entrada.query.filter(
        Entrada.membro_id == id,
        func.strftime("%Y", Entrada.data) == str(ano)
    ).order_by(Entrada.data).all()

    for e in entradas_membro:
        setattr(e, 'categoria_nome', categoria_display(e.tipo, NOME_RECEITA_POR_CODIGO))

    total_ano = sum(e.valor or 0 for e in entradas_membro)

    return render_template(
        'recibo_membro.html',
        membro=membro,
        ano=ano,
        entradas=entradas_membro,
        total_ano=total_ano,
    )


@app.route('/orcamentos', methods=['GET', 'POST'])
@roles_required(*CARGOS_FINANCEIRO)
def orcamentos():
    hoje = date.today()

    if request.method == 'POST':
        try:
            ano = int(request.form['ano'])
            mes = int(request.form['mes'])
            tipo_categoria = request.form['tipo_categoria']
            categoria_codigo = request.form['categoria_codigo']
            valor_previsto = float(request.form['valor_previsto'])
        except (ValueError, KeyError):
            flash('Dados inválidos para o orçamento.', 'erro')
            return redirect(url_for('orcamentos'))

        existente = Orcamento.query.filter_by(
            ano=ano, mes=mes, categoria_codigo=categoria_codigo, tipo_categoria=tipo_categoria
        ).first()
        if existente:
            existente.valor_previsto = valor_previsto
            flash('Orçamento atualizado com sucesso!', 'sucesso')
        else:
            db.session.add(Orcamento(
                ano=ano, mes=mes, categoria_codigo=categoria_codigo,
                tipo_categoria=tipo_categoria, valor_previsto=valor_previsto
            ))
            flash('Orçamento definido com sucesso!', 'sucesso')
        db.session.commit()
        return redirect(url_for('orcamentos', ano=ano, mes=mes))

    ano = request.args.get('ano', type=int) or hoje.year
    mes = request.args.get('mes', type=int) or hoje.month
    comparativo = get_comparativo_orcamento(ano, mes)

    return render_template(
        'orcamentos.html',
        ano=ano,
        mes=mes,
        comparativo=comparativo,
        catEntrada=CONTAS_RECEITA,
        catSaida=CONTAS_SAIDA,
    )


@app.route('/excluir-orcamento/<int:id>', methods=['POST'])
@roles_required(*CARGOS_FINANCEIRO)
def excluir_orcamento(id):
    orcamento = Orcamento.query.get_or_404(id)
    ano, mes = orcamento.ano, orcamento.mes
    db.session.delete(orcamento)
    db.session.commit()
    flash('Orçamento removido com sucesso!', 'sucesso')
    return redirect(url_for('orcamentos', ano=ano, mes=mes))


@app.route('/usuarios', methods=['GET', 'POST'])
@roles_required('administrador')
def usuarios():
    if request.method == 'POST':
        nome = request.form.get('nome', '').strip()
        email = request.form.get('email', '').strip().lower()
        senha = request.form.get('senha', '')
        cargo = request.form.get('cargo', '')

        if not nome or not email or not senha or cargo not in CARGOS_VALIDOS:
            flash('Preencha nome, e-mail, senha e um cargo válido.', 'erro')
            return redirect(url_for('usuarios'))

        if Usuario.query.filter_by(email=email).first():
            flash('Já existe um usuário com esse e-mail.', 'erro')
            return redirect(url_for('usuarios'))

        novo_usuario = Usuario(
            nome=nome,
            email=email,
            senha=bcrypt.generate_password_hash(senha).decode('utf-8'),
            cargo=cargo,
            ativo=True,
        )
        db.session.add(novo_usuario)
        db.session.commit()
        flash('Usuário criado com sucesso!', 'sucesso')
        return redirect(url_for('usuarios'))

    todos_usuarios = Usuario.query.order_by(Usuario.nome).all()
    return render_template('usuarios.html', usuarios=todos_usuarios, cargos=CARGOS_VALIDOS)


@app.route('/editar-usuario/<int:id>', methods=['GET', 'POST'])
@roles_required('administrador')
def editar_usuario(id):
    usuario = Usuario.query.get_or_404(id)

    if request.method == 'POST':
        novo_email = request.form.get('email', '').strip().lower()
        if novo_email != usuario.email and Usuario.query.filter_by(email=novo_email).first():
            flash('Já existe um usuário com esse e-mail.', 'erro')
            return redirect(url_for('editar_usuario', id=id))

        cargo_novo = request.form.get('cargo', usuario.cargo)
        if usuario.id_usuario == session.get('usuario_id') and cargo_novo != 'administrador':
            flash('Você não pode remover seu próprio acesso de administrador.', 'erro')
            return redirect(url_for('editar_usuario', id=id))

        usuario.nome = request.form.get('nome', usuario.nome).strip() or usuario.nome
        usuario.email = novo_email
        usuario.cargo = cargo_novo

        nova_senha = request.form.get('senha', '').strip()
        if nova_senha:
            usuario.senha = bcrypt.generate_password_hash(nova_senha).decode('utf-8')

        db.session.commit()
        flash('Usuário atualizado com sucesso!', 'sucesso')
        return redirect(url_for('usuarios'))

    return render_template('editar_usuario.html', usuario=usuario, cargos=CARGOS_VALIDOS)


@app.route('/excluir-usuario/<int:id>', methods=['POST'])
@roles_required('administrador')
def excluir_usuario(id):
    if id == session.get('usuario_id'):
        flash('Você não pode desativar seu próprio usuário.', 'erro')
        return redirect(url_for('usuarios'))

    usuario = Usuario.query.get_or_404(id)
    usuario.ativo = False
    db.session.commit()
    flash('Usuário desativado com sucesso!', 'sucesso')
    return redirect(url_for('usuarios'))


@app.route('/historico', methods=['GET'])
@roles_required('administrador')
def historico():
    tabela_filtro = request.args.get('tabela', '')
    query = HistoricoAlteracao.query.order_by(HistoricoAlteracao.data_hora.desc())
    if tabela_filtro in ('entradas', 'saidas', 'transferencias'):
        query = query.filter_by(tabela=tabela_filtro)
    registros = query.limit(200).all()

    historico_formatado = []
    for h in registros:
        antes = json.loads(h.dados_antes) if h.dados_antes else None
        depois = json.loads(h.dados_depois) if h.dados_depois else None
        campos_alterados = []
        if antes and depois:
            for campo in depois:
                if antes.get(campo) != depois.get(campo):
                    campos_alterados.append({'campo': campo, 'antes': antes.get(campo), 'depois': depois.get(campo)})
        historico_formatado.append({
            'registro': h,
            'antes': antes,
            'depois': depois,
            'campos_alterados': campos_alterados,
        })

    return render_template('historico.html', historico=historico_formatado, tabela_filtro=tabela_filtro)


@app.route('/relatorio', methods=['GET'])
@roles_required(*CARGOS_DETALHE)
def relatorio():
    # 1. Obter Parâmetros do Filtro
    start_date_str = request.args.get('start_date')
    end_date_str = request.args.get('end_date')
    tipo_filtro = request.args.get('tipo', '').upper()  # ENTRADA, SAIDA ou vazio
    categorias_selecionadas = [c for c in request.args.getlist('categorias') if c.strip()]


    # 2. Conversão e Validação de Datas
    start_date = None
    end_date = None
    try:
        if start_date_str:
            start_date = datetime.strptime(start_date_str, '%Y-%m-%d').date()
    except ValueError:
        flash('Data inicial inválida.', 'erro')
        start_date_str = None

    try:
        if end_date_str:
            end_date = datetime.strptime(end_date_str, '%Y-%m-%d').date()
    except ValueError:
        flash('Data final inválida.', 'erro')
        end_date_str = None

    # Se não houver filtro, define o período como o mês atual
    if not start_date and not end_date and not tipo_filtro and not categorias_selecionadas:
        hoje = date.today()
        start_date = hoje.replace(day=1)
        start_date_str = start_date.strftime('%Y-%m-%d')
        end_date = hoje
        end_date_str = end_date.strftime('%Y-%m-%d')

    # 3. Consultas Base
    entradas_query = Entrada.query
    saidas_query = Saida.query

    if start_date:
        entradas_query = entradas_query.filter(func.date(Entrada.data) >= start_date)
        saidas_query = saidas_query.filter(func.date(Saida.data) >= start_date)
    if end_date:
        entradas_query = entradas_query.filter(func.date(Entrada.data) <= end_date)
        saidas_query = saidas_query.filter(func.date(Saida.data) <= end_date)

    if categorias_selecionadas:
        if tipo_filtro in ('ENTRADA', ''):
            entradas_query = entradas_query.filter(Entrada.tipo.in_(categorias_selecionadas))
        if tipo_filtro in ('SAIDA', ''):
            saidas_query = saidas_query.filter(Saida.tipo.in_(categorias_selecionadas))

    # 4. Execução dos Filtros
    if tipo_filtro == 'ENTRADA':
        saidas = []
        entradas = entradas_query.all()
    elif tipo_filtro == 'SAIDA':
        entradas = []
        saidas = saidas_query.all()
    else:
        entradas = entradas_query.all()
        saidas = saidas_query.all()

    # 5. Combinação das Movimentações (mostra código + nome, ex: '1.1 - Dízimos')
    movimentacoes_list = []
    for e in entradas:
        movimentacoes_list.append({
            'tipo': 'ENTRADA',
            'categoria': categoria_display(e.tipo, NOME_RECEITA_POR_CODIGO),
            'valor': e.valor,
            'data': e.data,
            'data_pagamento': None,
            'data_vencimento': None,
            'forma': e.forma_pagamento,
            'descricao': e.descricao,
            'id': e.id_entrada
        })

    for s in saidas:
        movimentacoes_list.append({
            'tipo': 'SAÍDA',
            'categoria': categoria_display(s.tipo, NOME_SAIDA_POR_CODIGO),
            'valor': s.valor,
            'data': s.data,
            'data_pagamento': s.data_pagamento,
            'data_vencimento': s.data_vencimento,
            'forma': s.forma_pagamento,
            'descricao': s.descricao,
            'id': s.id_saida
        })

    movimentacoes = sorted(movimentacoes_list, key=lambda x: x['data'], reverse=True)

    # 6. Cálculos e Gráficos
    total_entradas = sum(m['valor'] for m in movimentacoes if m['tipo'] == 'ENTRADA')
    total_saidas = sum(m['valor'] for m in movimentacoes if m['tipo'] == 'SAÍDA')

    resumo_tipo = defaultdict(float)
    resumo_pagamento = defaultdict(float)

    for m in movimentacoes:
        chave = f"{m['tipo']}: {m['categoria']}"
        resumo_tipo[chave] += m['valor']
        resumo_pagamento[m['forma']] += m['valor']

    # 🔹 Correção — Garante que sempre haja dados válidos
    resumo_tipo_chart = dict(resumo_tipo) if resumo_tipo else {}
    resumo_pagamento_chart = dict(resumo_pagamento) if resumo_pagamento else {}
    dados_tendencia = calcular_dados_tendencia() or {"labels": [], "receitas": [], "despesas": []}

    # 🔹 Garante que filtros e títulos sejam sempre válidos
    categorias_selecionadas = categorias_selecionadas or []
    tipo_filtro = tipo_filtro or ""
    titulo = (
        f"De {start_date.strftime('%d/%m/%Y')} a {end_date.strftime('%d/%m/%Y')}" if start_date and end_date else
        f"A partir de {start_date.strftime('%d/%m/%Y')}" if start_date else
        f"Até {end_date.strftime('%d/%m/%Y')}" if end_date else
        "Todo o Período"
    )

    # 🔹 Renderização final segura
    return render_template(
        'relatorio.html',
        movimentacoes=movimentacoes,
        titulo_periodo=titulo,
        start_date_value=start_date_str or "",
        end_date_value=end_date_str or "",
        tipo_value=tipo_filtro,
        categorias_selecionadas=categorias_selecionadas,
        catEntrada=CONTAS_RECEITA,
        catSaida=CONTAS_SAIDA,
        total_entradas=total_entradas or 0,
        total_saidas=total_saidas or 0,
        saldo=(total_entradas or 0) - (total_saidas or 0),
        resumo_tipo_chart=resumo_tipo_chart,
        resumo_pagamento_chart=resumo_pagamento_chart,
        dados_tendencia=dados_tendencia,
    )



# ==============================
# ROTAS DO CHATBOT
# ==============================

@app.route('/api/finance_summary', methods=['GET'])
def finance_summary_api():
    """
    Rota API para fornecer o resumo financeiro em tempo real para o LLM.
    Agora aceita o parâmetro 'query' para extrair a data de referência.
    """
    if 'usuario' not in session:
        return jsonify({"error": "Não autenticado"}), 401
    
    user_query = request.args.get('query', '')
    
    # Tenta extrair a data da consulta do usuário
    date_info = extract_date_from_query(user_query)
    
    if date_info:
        ref_year, ref_month = date_info
    else:
        # Se nenhuma data for encontrada, usa o mês e ano atuais como padrão
        hoje = date.today()
        ref_year, ref_month = hoje.year, hoje.month
        
    try:
        # Chama a função que busca dados reais do banco de dados para o período
        summary_json_string = get_chatbot_context_json(ref_year, ref_month)
        
        # Retorna o JSON string EMBALADO para o frontend
        return jsonify({"financial_context": summary_json_string})
    except Exception as e:
        # Em caso de erro (ex: mês inválido), retorna um erro.
        print(f"Erro ao buscar contexto financeiro: {e}")
        return jsonify({"error": "Erro ao buscar dados. Tente novamente."}), 500

@app.route('/api/chat', methods=['POST'])
def api_chat():
    """Recebe a pergunta do usuário, monta o contexto financeiro e chama a Gemini
    inteiramente no servidor (a chave de API nunca é exposta ao navegador)."""
    if 'usuario' not in session:
        return jsonify({"error": "Não autenticado"}), 401

    data = request.get_json(silent=True) or {}
    user_query = (data.get('query') or '').strip()
    if not user_query:
        return jsonify({"status": "error", "message": "Campo 'query' é obrigatório."}), 400

    date_info = extract_date_from_query(user_query)
    if date_info:
        ref_year, ref_month = date_info
    else:
        hoje = date.today()
        ref_year, ref_month = hoje.year, hoje.month

    try:
        context = json.loads(get_chatbot_context_json(ref_year, ref_month))
        system_prompt = build_chat_system_prompt(context, user_query)
        _, text = call_gemini(user_query, system_instruction=system_prompt)
        return jsonify({"status": "success", "text": text or "Desculpe, a resposta da IA está vazia ou malformada."})
    except RuntimeError as e:
        return jsonify({"status": "error", "message": str(e)}), 500
    except Exception as e:
        print(f"Erro no chat: {e}")
        return jsonify({"status": "error", "message": "Erro ao processar sua pergunta. Tente novamente."}), 500

@app.route('/export/saidas/csv', methods=['GET'])
@roles_required(*CARGOS_DETALHE)
def export_saidas_csv():
    cols = [c.name for c in Saida.__table__.columns]
    si = io.StringIO()
    writer = csv.writer(si)
    writer.writerow(cols)

    for s in Saida.query.order_by(Saida.id_saida).all():
        row = []
        for col in cols:
            v = getattr(s, col)
            if isinstance(v, datetime):
                row.append(v.strftime('%Y-%m-%d %H:%M:%S'))
            else:
                row.append('' if v is None else str(v))
        writer.writerow(row)

    output = make_response(si.getvalue())
    output.headers['Content-Disposition'] = 'attachment; filename=saidas.csv'
    output.headers['Content-Type'] = 'text/csv; charset=utf-8'
    return output


@app.route('/export/entradas/csv', methods=['GET'])
@roles_required(*CARGOS_DETALHE)
def export_entradas_csv():
    cols = [c.name for c in Entrada.__table__.columns]
    si = io.StringIO()
    writer = csv.writer(si)
    writer.writerow(cols)

    for e in Entrada.query.order_by(Entrada.id_entrada).all():
        row = []
        for col in cols:
            v = getattr(e, col)
            if isinstance(v, datetime):
                row.append(v.strftime('%Y-%m-%d %H:%M:%S'))
            else:
                row.append('' if v is None else str(v))
        writer.writerow(row)

    output = make_response(si.getvalue())
    output.headers['Content-Disposition'] = 'attachment; filename=entradas.csv'
    output.headers['Content-Type'] = 'text/csv; charset=utf-8'
    return output


# ==============================
# CRIAR TABELAS E USUÁRIO INICIAL
# ==============================
CARGOS_VALIDOS = ('administrador', 'tesoureiro', 'secretario', 'pastor', 'consulta')
MIGRACAO_CARGOS_LEGADO = {'admin': 'administrador'}

def _add_column_if_missing(table, column, coltype):
    """Adiciona uma coluna a uma tabela existente do SQLite, se ainda não existir
    (db.create_all() só cria tabelas novas, não altera as já existentes)."""
    with db.engine.connect() as conn:
        cols = [row[1] for row in conn.exec_driver_sql(f"PRAGMA table_info('{table}')")]
        if column not in cols:
            conn.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN {column} {coltype}")
            conn.commit()
            print(f"Migração: coluna '{column}' adicionada em '{table}'.")


with app.app_context():
    db.create_all()

    # Migração: novas colunas em entradas/saidas para vínculo com conta e membro
    _add_column_if_missing('entradas', 'conta_id', 'INTEGER')
    _add_column_if_missing('entradas', 'membro_id', 'INTEGER')
    _add_column_if_missing('saidas', 'conta_id', 'INTEGER')
    _add_column_if_missing('usuarios', 'ativo', 'BOOLEAN DEFAULT 1')

    # Garante ao menos uma conta padrão e migra lançamentos antigos sem conta definida
    if not Conta.query.first():
        db.session.add(Conta(nome='Caixa', tipo='caixa', saldo_inicial=0.0, ativa=True))
        db.session.commit()
        print("Conta padrão 'Caixa' criada.")

    conta_padrao = Conta.query.order_by(Conta.id_conta).first()
    Entrada.query.filter_by(conta_id=None).update({Entrada.conta_id: conta_padrao.id_conta})
    Saida.query.filter_by(conta_id=None).update({Saida.conta_id: conta_padrao.id_conta})
    db.session.commit()

    # Normaliza cargos de versões antigas (ex.: 'Admin' -> 'administrador') para o controle de acesso funcionar
    for u in Usuario.query.all():
        cargo_normalizado = (u.cargo or '').strip().lower()
        if cargo_normalizado not in CARGOS_VALIDOS:
            novo_cargo = MIGRACAO_CARGOS_LEGADO.get(cargo_normalizado)
            if novo_cargo:
                print(f"Normalizando cargo do usuário {u.email}: '{u.cargo}' -> '{novo_cargo}'")
                u.cargo = novo_cargo
    db.session.commit()

    if not Usuario.query.first():
        try:
            admin_email = os.environ.get('ADMIN_EMAIL', 'admin@igreja.com')
            admin_password = os.environ.get('ADMIN_PASSWORD')
            if not admin_password:
                admin_password = secrets.token_urlsafe(9)
                print(f"AVISO: variável de ambiente ADMIN_PASSWORD não definida. "
                      f"Senha gerada para o admin inicial ({admin_email}): {admin_password} "
                      f"— anote agora, ela não será exibida novamente.")
            hashed_password = bcrypt.generate_password_hash(admin_password).decode('utf-8')
            novo_usuario = Usuario(nome="Admin", email=admin_email, senha=hashed_password, cargo="administrador")
            db.session.add(novo_usuario)
            db.session.commit()
            print(f"Usuário Admin ({admin_email}) criado.")
        except Exception as e:
            print(f"Erro ao criar usuário admin: {e}")


# ==============================
# INICIALIZAÇÃO
# ==============================
if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    debug_mode = os.environ.get('FLASK_DEBUG', 'false').strip().lower() in ('1', 'true', 'yes')
    if debug_mode:
        app.run(host='0.0.0.0', port=port, debug=True)
    else:
        from waitress import serve
        print(f"Servindo com Waitress (produção) em http://0.0.0.0:{port}")
        serve(app, host='0.0.0.0', port=port)
