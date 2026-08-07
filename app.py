from flask import Flask, render_template, request, redirect, url_for, session, flash, jsonify, make_response, send_from_directory, abort
import csv
import io
import os
import shutil
import glob
import requests
from flask_sqlalchemy import SQLAlchemy
from flask_bcrypt import Bcrypt
from flask_wtf import CSRFProtect
from flask_limiter import Limiter
from datetime import datetime, date, timedelta
from sqlalchemy import func
from collections import defaultdict # Novo: Para agregação de dados no relatório
from functools import wraps
import json
import re
import secrets
import smtplib
import threading
import time
from email.mime.text import MIMEText

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
app.config['MAX_CONTENT_LENGTH'] = 8 * 1024 * 1024  # 8 MB, limite para anexos de comprovante

# ==============================
# DADOS DA IGREJA (usados no recibo de doação e futuros documentos formais)
# ==============================
IGREJA_NOME = "Igreja Pentecostal Jesus Cristo é a Salvação das Nações"
IGREJA_CNPJ = "07.071.168/0001-37"
IGREJA_ENDERECO = "R. Ubiratã, 101 - Parque Pirajussara, Embu das Artes - SP, 06815-030"
IGREJA_CIDADE_UF = "Embu das Artes - SP"

# ==============================
# CONFIGURAÇÃO DE E-MAIL (avisos de vencimento de contas a pagar)
# ==============================
SMTP_HOST = os.environ.get('SMTP_HOST', '')
SMTP_PORT = int(os.environ.get('SMTP_PORT', '587'))
SMTP_USER = os.environ.get('SMTP_USER', '')
SMTP_SENHA = os.environ.get('SMTP_SENHA', '')
SMTP_REMETENTE = os.environ.get('SMTP_REMETENTE', SMTP_USER)
SMTP_DESTINATARIO = os.environ.get('SMTP_DESTINATARIO', '')

db = SQLAlchemy(app)
bcrypt = Bcrypt(app)
csrf = CSRFProtect(app)


def get_ip_real():
    """Resolve o IP real do cliente por trás do túnel Cloudflare (que usa CF-Connecting-IP),
    com fallback para X-Forwarded-For e, por fim, o IP direto da conexão."""
    return (
        request.headers.get('CF-Connecting-IP')
        or request.headers.get('X-Forwarded-For', request.remote_addr or '').split(',')[0].strip()
        or request.remote_addr
    )


limiter = Limiter(get_ip_real, app=app, default_limits=[])

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
    membro_id = db.Column(db.Integer, db.ForeignKey('membros.id_membro'))
    pendente_aprovacao = db.Column(db.Boolean, default=False)


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
    comprovante_arquivo = db.Column(db.String(255))


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
    comprovante_arquivo = db.Column(db.String(255))


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
    criado_via_cadastro = db.Column(db.Boolean, default=False)

    # Ficha cadastral — dados pessoais
    foto_arquivo = db.Column(db.String(255))
    data_nascimento = db.Column(db.DateTime)
    rg = db.Column(db.String(20))
    estado_civil = db.Column(db.String(30))
    nome_conjuge = db.Column(db.String(100))
    qtd_filhos = db.Column(db.Integer, default=0)
    nomes_filhos = db.Column(db.Text)
    trabalha_atualmente = db.Column(db.Boolean, default=False)

    # Ficha cadastral — endereço
    cep = db.Column(db.String(10))
    endereco = db.Column(db.String(255))

    # Ficha cadastral — dados ministeriais
    data_batismo = db.Column(db.DateTime)
    data_entrada_ministerio = db.Column(db.DateTime)
    data_saida_ministerio = db.Column(db.DateTime)
    funcao_ministerial = db.Column(db.String(50))


class PlanoConta(db.Model):
    __tablename__ = "plano_contas"
    id_plano = db.Column(db.Integer, primary_key=True)
    codigo = db.Column(db.String(20), nullable=False, unique=True)
    nome = db.Column(db.String(100), nullable=False)
    tipo = db.Column(db.String(10), nullable=False)  # 'RECEITA' | 'SAIDA'
    ativo = db.Column(db.Boolean, default=True)


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


class LoginLog(db.Model):
    __tablename__ = "login_logs"
    id_log = db.Column(db.Integer, primary_key=True)
    email_tentativa = db.Column(db.String(100))
    sucesso = db.Column(db.Boolean, default=False)
    motivo = db.Column(db.String(100))  # ex.: 'senha incorreta', 'usuário desativado', 'sucesso'
    ip = db.Column(db.String(45))
    data_hora = db.Column(db.DateTime, default=datetime.now)


# ==============================
# PLANO DE CONTAS (editável em /plano-contas; seed inicial usado só na primeira execução)
# ==============================
SEED_PLANO_RECEITA = [
    {"codigo": "1.1", "nome": "Dízimos"},
    {"codigo": "1.2", "nome": "Ofertas"},
    {"codigo": "1.3", "nome": "Contribuições"},
    {"codigo": "1.4", "nome": "Eventos"},
    {"codigo": "1.5", "nome": "Doações Especiais"},
    {"codigo": "1.6", "nome": "Outras Receitas"},
]

SEED_PLANO_SAIDA = [
    {"codigo": "2.1", "nome": "Despesas com Pessoal"},
    {"codigo": "2.2", "nome": "Despesas Operacionais Fixas"},
    {"codigo": "2.3", "nome": "Despesas Administrativas"},
    {"codigo": "2.4", "nome": "Despesas de Manutenção"},
    {"codigo": "2.5", "nome": "Investimentos/Ações Sociais"},
    {"codigo": "Outro", "nome": "Outro"},
]

FORMAS_PAGAMENTO_ENTRADA = ['Dinheiro', 'Pix', 'Débito', 'Crédito', 'Transferência']
FORMAS_PAGAMENTO_SAIDA = ['Dinheiro', 'Pix', 'Débito', 'Crédito', 'Boleto']

ITENS_POR_PAGINA = 25


def contas_receita(somente_ativas=True):
    query = PlanoConta.query.filter_by(tipo='RECEITA')
    if somente_ativas:
        query = query.filter_by(ativo=True)
    return [{"codigo": c.codigo, "nome": c.nome} for c in query.order_by(PlanoConta.codigo).all()]


def contas_saida(somente_ativas=True):
    query = PlanoConta.query.filter_by(tipo='SAIDA')
    if somente_ativas:
        query = query.filter_by(ativo=True)
    return [{"codigo": c.codigo, "nome": c.nome} for c in query.order_by(PlanoConta.codigo).all()]


def nome_receita_por_codigo():
    # Inclui categorias desativadas para que lançamentos antigos continuem exibindo o nome correto.
    return {c['codigo']: c['nome'] for c in contas_receita(somente_ativas=False)}


def nome_saida_por_codigo():
    return {c['codigo']: c['nome'] for c in contas_saida(somente_ativas=False)}


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


def registrar_login_log(email, sucesso, motivo=''):
    """Registra uma tentativa de login (sucesso ou falha) com o IP real do cliente."""
    db.session.add(LoginLog(
        email_tentativa=email,
        sucesso=sucesso,
        motivo=motivo,
        ip=get_ip_real(),
    ))
    db.session.commit()


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


def saldo_suficiente_para_saida(conta_id, valor, conta_id_original=None, valor_original=None, estava_pago=False):
    """Verifica se a conta tem saldo suficiente para um pagamento de `valor`.
    O saldo de cada conta é isolado — dinheiro no banco nunca é considerado
    automaticamente disponível no caixa, e vice-versa.
    Ao editar uma saída já paga, informe conta_id_original/valor_original/estava_pago
    com os valores de ANTES da edição: se ela já debitava essa mesma conta, o valor
    que ela mesma já havia debitado é devolvido, para não contá-la duas vezes.
    Retorna (ok: bool, saldo_disponivel: float, conta: Conta|None)."""
    conta = Conta.query.get(conta_id) if conta_id else None
    if not conta:
        return False, 0.0, None
    saldo = calcular_saldo_conta(conta)
    if estava_pago and conta_id_original == conta_id:
        saldo += valor_original or 0
    return (saldo >= valor), saldo, conta


def validar_reducao_entrada(conta_id_original, valor_original, conta_id_novo=None, valor_novo=0):
    """Verifica se reduzir/remover uma entrada não deixaria a conta original com saldo
    negativo — cobre tanto excluir uma entrada (conta_id_novo=None) quanto editá-la
    (diminuir o valor ou trocar de conta). Esse dinheiro pode já ter sido usado em
    pagamentos pagos dessa mesma conta.
    Retorna (ok: bool, mensagem_erro: str|None)."""
    if not conta_id_original:
        return True, None
    conta = Conta.query.get(conta_id_original)
    if not conta:
        return True, None
    saldo_atual = calcular_saldo_conta(conta)
    contribuicao_nesta_conta = valor_novo if conta_id_novo == conta_id_original else 0
    saldo_resultante = saldo_atual - (valor_original or 0) + contribuicao_nesta_conta
    if saldo_resultante < 0:
        return False, (
            f'Esta operação deixaria o saldo de "{conta.nome}" negativo (R$ {saldo_resultante:.2f}), '
            'pois esse dinheiro já foi usado em pagamentos dessa conta.'
        )
    return True, None


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
        setattr(s, 'categoria_nome', categoria_display(s.tipo, nome_saida_por_codigo()))

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


def smtp_configurado():
    return bool(SMTP_HOST and SMTP_USER and SMTP_SENHA and SMTP_DESTINATARIO)


def enviar_email(destinatario, assunto, corpo_html):
    """Envia um e-mail via SMTP usando as credenciais configuradas em variáveis de ambiente.
    Retorna True se enviado com sucesso, False caso contrário (registra o erro no console,
    já que este envio roda em segundo plano sem usuário para ver um flash)."""
    if not smtp_configurado():
        return False
    try:
        msg = MIMEText(corpo_html, 'html', 'utf-8')
        msg['Subject'] = assunto
        msg['From'] = SMTP_REMETENTE
        msg['To'] = destinatario
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=15) as servidor:
            servidor.starttls()
            servidor.login(SMTP_USER, SMTP_SENHA)
            servidor.sendmail(SMTP_REMETENTE, [destinatario], msg.as_string())
        return True
    except Exception as e:
        print(f"Erro ao enviar e-mail de aviso de vencimento: {e}")
        return False


def montar_corpo_aviso_vencimento(atrasadas, proximas):
    def linhas(lista):
        if not lista:
            return '<p style="color:#6b7280;font-style:italic;">Nenhuma.</p>'
        html = '<table style="width:100%;border-collapse:collapse;font-size:14px;">'
        for s in lista:
            html += (
                '<tr style="border-bottom:1px solid #e5e7eb;">'
                f'<td style="padding:6px;">{s.categoria_nome}</td>'
                f'<td style="padding:6px;">{s.descricao or "-"}</td>'
                f'<td style="padding:6px;">{s.data_vencimento.strftime("%d/%m/%Y") if s.data_vencimento else "-"}</td>'
                f'<td style="padding:6px;text-align:right;">R$ {s.valor:.2f}</td>'
                '</tr>'
            )
        html += '</table>'
        return html

    return f"""
    <div style="font-family:Arial,sans-serif;max-width:600px;margin:0 auto;">
        <h2 style="color:#1f2937;">Aviso de Contas a Pagar — {IGREJA_NOME}</h2>
        <h3 style="color:#dc2626;">Atrasadas ({len(atrasadas)})</h3>
        {linhas(atrasadas)}
        <h3 style="color:#d97706;">Vencendo nos próximos dias ({len(proximas)})</h3>
        {linhas(proximas)}
        <p style="color:#6b7280;font-size:12px;margin-top:24px;">
            Mensagem automática do sistema financeiro. Acesse o sistema para marcar contas como pagas.
        </p>
    </div>
    """


def _caminho_ultimo_aviso():
    return os.path.join(app.instance_path, 'ultimo_aviso_vencimento.txt')


def verificar_e_enviar_avisos_vencimento(forcar=False):
    """Verifica contas a pagar atrasadas/próximas do vencimento e envia um e-mail resumo,
    no máximo uma vez por dia (a menos que forcar=True). Retorna um dicionário com o
    resultado, usado tanto pelo laço em segundo plano quanto pela rota de envio manual."""
    if not smtp_configurado():
        return {'enviado': False, 'motivo': 'E-mail não configurado.'}

    hoje_str = date.today().isoformat()
    caminho = _caminho_ultimo_aviso()
    if not forcar and os.path.exists(caminho):
        with open(caminho, 'r', encoding='utf-8') as f:
            if f.read().strip() == hoje_str:
                return {'enviado': False, 'motivo': 'Aviso já enviado hoje.'}

    contas = get_contas_a_pagar()
    atrasadas, proximas = contas['atrasadas'], contas['proximas']
    if not atrasadas and not proximas:
        with open(caminho, 'w', encoding='utf-8') as f:
            f.write(hoje_str)
        return {'enviado': False, 'motivo': 'Nenhuma conta atrasada ou próxima do vencimento.'}

    assunto = f"[{IGREJA_NOME}] Aviso: {len(atrasadas)} conta(s) atrasada(s), {len(proximas)} vencendo em breve"
    corpo = montar_corpo_aviso_vencimento(atrasadas, proximas)
    sucesso = enviar_email(SMTP_DESTINATARIO, assunto, corpo)
    if sucesso:
        with open(caminho, 'w', encoding='utf-8') as f:
            f.write(hoje_str)
        return {'enviado': True, 'motivo': f'E-mail enviado para {SMTP_DESTINATARIO}.'}
    return {'enviado': False, 'motivo': 'Falha ao enviar e-mail (veja o console do servidor).'}


def _loop_avisos_vencimento():
    """Roda em uma thread em segundo plano, verificando uma vez por hora se já é
    hora de disparar o aviso diário de vencimento (a própria função garante que só
    envia uma vez por dia)."""
    while True:
        try:
            with app.app_context():
                verificar_e_enviar_avisos_vencimento()
        except Exception as e:
            print(f"Erro no laço de avisos de vencimento: {e}")
        time.sleep(3600)


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
            percentual = (realizado / previsto * 100) if previsto else (100.0 if realizado else 0.0)
            linhas.append({
                'codigo': codigo,
                'nome': conta['nome'],
                'previsto': previsto,
                'realizado': realizado,
                'diferenca': realizado - previsto,
                'percentual': percentual,
                'tem_orcamento': previsto > 0 or realizado > 0,
            })
        return linhas

    linhas_receita = montar_linhas(contas_receita(), 'RECEITA', realizado_receita)
    linhas_saida = montar_linhas(contas_saida(), 'SAIDA', realizado_saida)

    return {
        'receitas': linhas_receita,
        'saidas': linhas_saida,
        'total_previsto_receita': sum(l['previsto'] for l in linhas_receita),
        'total_realizado_receita': sum(l['realizado'] for l in linhas_receita),
        'total_previsto_saida': sum(l['previsto'] for l in linhas_saida),
        'total_realizado_saida': sum(l['realizado'] for l in linhas_saida),
    }


def get_resumo_anual(ano):
    """Consolidado do ano inteiro: totais, evolução mês a mês e total por categoria."""
    entradas_ano = Entrada.query.filter(func.strftime("%Y", Entrada.data) == str(ano)).all()
    saidas_ano = Saida.query.filter(func.strftime("%Y", Saida.data) == str(ano)).all()

    total_entradas = sum(e.valor or 0 for e in entradas_ano)
    total_saidas = sum(s.valor or 0 for s in saidas_ano)

    por_categoria_receita = defaultdict(float)
    for e in entradas_ano:
        por_categoria_receita[categoria_display(e.tipo, nome_receita_por_codigo())] += e.valor or 0

    por_categoria_saida = defaultdict(float)
    for s in saidas_ano:
        por_categoria_saida[categoria_display(s.tipo, nome_saida_por_codigo())] += s.valor or 0

    labels, receitas_mes, despesas_mes = [], [], []
    for mes in range(1, 13):
        labels.append(f"{mes:02d}/{ano % 100:02d}")
        receitas_mes.append(sum((e.valor or 0) for e in entradas_ano if e.data.month == mes))
        despesas_mes.append(sum((s.valor or 0) for s in saidas_ano if s.data.month == mes))

    return {
        'total_entradas': total_entradas,
        'total_saidas': total_saidas,
        'saldo': total_entradas - total_saidas,
        'por_categoria_receita': dict(sorted(por_categoria_receita.items(), key=lambda x: -x[1])),
        'por_categoria_saida': dict(sorted(por_categoria_saida.items(), key=lambda x: -x[1])),
        'labels': labels,
        'receitas_mes': receitas_mes,
        'despesas_mes': despesas_mes,
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
        tipo = categoria_display(e.tipo, nome_receita_por_codigo()) or 'Outro'
        resumo_entradas_tipo[tipo] = resumo_entradas_tipo.get(tipo, 0) + (e.valor or 0)

    # Resumos por Tipo (Saídas)
    resumo_saidas_tipo = {}
    for s in saidas_mes:
        tipo = categoria_display(s.tipo, nome_saida_por_codigo()) or 'Outro'
        resumo_saidas_tipo[tipo] = resumo_saidas_tipo.get(tipo, 0) + (s.valor or 0)

    # Adiciona lista de movimentações detalhada
    lista_movimentacoes_detalhada = []
    for e in entradas_mes:
        lista_movimentacoes_detalhada.append({
            "tipo": "Entrada",
            "categoria": categoria_display(e.tipo, nome_receita_por_codigo()),
            "valor": f"R$ {e.valor:.2f}",
            "data_completa": e.data.strftime('%d/%m/%Y'),
            "descricao": e.descricao
        })
    for s in saidas_mes:
        lista_movimentacoes_detalhada.append({
            "tipo": "Saida",
            "categoria": categoria_display(s.tipo, nome_saida_por_codigo()),
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
@limiter.limit("5 per minute", methods=["POST"])
def login():
    if request.method == 'POST':
        email = request.form['email']
        senha = request.form['senha']
        usuario = Usuario.query.filter_by(email=email).first()
        if usuario and not usuario.ativo:
            if usuario.pendente_aprovacao:
                registrar_login_log(email, False, 'cadastro pendente de aprovação')
                flash('Seu cadastro ainda está aguardando aprovação de um administrador.', 'aviso')
            else:
                registrar_login_log(email, False, 'usuário desativado')
                flash('Este usuário foi desativado.', 'erro')
            return redirect(url_for('login'))
        if usuario and bcrypt.check_password_hash(usuario.senha, senha):
            session['usuario'] = usuario.nome
            session['cargo'] = usuario.cargo
            session['usuario_id'] = usuario.id_usuario
            session['membro_id'] = usuario.membro_id
            registrar_login_log(email, True, 'sucesso')
            return redirect(url_for('dashboard'))
        else:
            registrar_login_log(email, False, 'senha incorreta' if usuario else 'e-mail não encontrado')
            flash('Email ou senha incorretos!', 'erro')
            return redirect(url_for('login'))
    return render_template('login.html')

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))


@app.route('/cadastro', methods=['GET', 'POST'])
@limiter.limit("5 per minute", methods=["POST"])
def cadastro():
    if 'usuario' in session:
        return redirect(url_for('dashboard'))

    if request.method == 'POST':
        nome = request.form.get('nome', '').strip()
        email = request.form.get('email', '').strip().lower()
        senha = request.form.get('senha', '')
        confirmar_senha = request.form.get('confirmar_senha', '')
        cpf = request.form.get('cpf', '').strip()
        telefone = request.form.get('telefone', '').strip()

        if not nome or not email or not senha:
            flash('Preencha nome, e-mail e senha.', 'erro')
            return redirect(url_for('cadastro'))
        if len(senha) < 6:
            flash('A senha deve ter pelo menos 6 caracteres.', 'erro')
            return redirect(url_for('cadastro'))
        if senha != confirmar_senha:
            flash('As senhas não coincidem.', 'erro')
            return redirect(url_for('cadastro'))
        if Usuario.query.filter_by(email=email).first():
            flash('Já existe uma conta com esse e-mail.', 'erro')
            return redirect(url_for('cadastro'))

        membro = None
        if cpf:
            membro = Membro.query.filter_by(cpf=cpf, ativo=True).first()
        if not membro:
            membro = Membro.query.filter(func.lower(Membro.email) == email, Membro.ativo == True).first()

        if membro and Usuario.query.filter_by(membro_id=membro.id_membro).first():
            flash('Já existe uma conta vinculada a este membro. Se você esqueceu a senha, contate um administrador.', 'erro')
            return redirect(url_for('cadastro'))

        if not membro:
            membro = Membro(nome=nome, cpf=cpf or None, email=email, telefone=telefone or None, ativo=True, criado_via_cadastro=True)
            db.session.add(membro)
            db.session.flush()

        novo_usuario = Usuario(
            nome=nome,
            email=email,
            senha=bcrypt.generate_password_hash(senha).decode('utf-8'),
            cargo='membro',
            ativo=False,
            pendente_aprovacao=True,
            membro_id=membro.id_membro,
        )
        db.session.add(novo_usuario)
        db.session.commit()
        flash('Cadastro enviado! Aguarde a aprovação de um administrador para poder acessar o sistema.', 'sucesso')
        return redirect(url_for('login'))

    return render_template('cadastro.html')


@app.errorhandler(429)
def limite_excedido(e):
    flash('Muitas tentativas de login em pouco tempo. Aguarde um minuto e tente novamente.', 'erro')
    return redirect(url_for('login')), 429

@app.route('/dashboard')
@login_required
def dashboard():
    if session.get('cargo') == 'membro':
        return redirect(url_for('meu_portal'))

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

@app.route('/api/dashboard-resumo', methods=['GET'])
@login_required
def api_dashboard_resumo():
    """Resumo leve do dashboard, consultado periodicamente pelo navegador para
    atualizar os números na tela sem precisar recarregar a página."""
    if session.get('cargo') == 'membro':
        abort(403)
    summary = get_finance_summary()
    dados = {
        "total_entradas": float(summary['total_entradas']),
        "total_saidas": float(summary['total_saidas']),
        "saldo": float(summary['saldo_atual']),
        "total_dia": float(summary['total_entradas_hoje']),
    }
    if session.get('cargo') != 'consulta':
        contas_a_pagar = get_contas_a_pagar()
        dados["total_atrasadas"] = len(contas_a_pagar['atrasadas'])
    return jsonify(dados)


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
        arquivo = request.files.get('comprovante')
        nome_arquivo = salvar_comprovante(arquivo, 'entradas', nova_entrada.id_entrada)
        if arquivo and arquivo.filename and not nome_arquivo:
            flash('Tipo de arquivo não permitido para comprovante. Use PNG, JPG, WEBP ou PDF.', 'aviso')
        elif nome_arquivo:
            nova_entrada.comprovante_arquivo = nome_arquivo
        registrar_historico('entradas', nova_entrada.id_entrada, 'criar', depois=snapshot_registro(nova_entrada))
        db.session.commit()
        flash('Entrada registrada com sucesso!', 'sucesso')
        return redirect(url_for('entradas'))

    # 🔹 Carrega as entradas paginadas (mais recentes primeiro), filtradas por período
    # (mês/ano atual por padrão) para não sobrecarregar a tela conforme o histórico cresce
    hoje = date.today()
    mostrar_todos = request.args.get('todos', '') == '1'
    ano_filtro = request.args.get('ano', type=int) or hoje.year
    mes_filtro = request.args.get('mes', type=int) or hoje.month

    consulta = Entrada.query
    if not mostrar_todos:
        consulta = consulta.filter(func.strftime('%Y-%m', Entrada.data) == f'{ano_filtro:04d}-{mes_filtro:02d}')

    pagina = request.args.get('page', 1, type=int)
    paginacao = consulta.order_by(Entrada.data.desc()).paginate(page=pagina, per_page=ITENS_POR_PAGINA, error_out=False)
    todas_entradas = paginacao.items
    contas_por_id = {c.id_conta: c.nome for c in Conta.query.all()}
    membros_por_id = {m.id_membro: m.nome for m in Membro.query.all()}

    # Anexa atributos temporários aos objetos de entrada para uso no template
    for e in todas_entradas:
        codigo = (e.tipo or '').strip()
        setattr(e, 'categoria_codigo', codigo)
        setattr(e, 'categoria_nome', nome_receita_por_codigo().get(codigo, codigo))
        setattr(e, 'conta_nome', contas_por_id.get(e.conta_id, '-'))
        setattr(e, 'membro_nome', membros_por_id.get(e.membro_id, '-'))

    return render_template(
        'entradas.html',
        entradas=todas_entradas,
        paginacao=paginacao,
        contas_receita=contas_receita(),
        formas_pagamento=FORMAS_PAGAMENTO_ENTRADA,
        contas=Conta.query.filter_by(ativa=True).order_by(Conta.nome).all(),
        membros=Membro.query.filter_by(ativo=True).order_by(Membro.nome).all(),
        ano_filtro=ano_filtro,
        mes_filtro=mes_filtro,
        mostrar_todos=mostrar_todos,
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

        if data_pagamento:
            ok, saldo_disponivel, conta = saldo_suficiente_para_saida(conta_id, valor)
            if not ok:
                nome_conta = conta.nome if conta else 'selecionada'
                flash(
                    f'Saldo insuficiente em "{nome_conta}" (disponível: R$ {saldo_disponivel:.2f}) para pagar R$ {valor:.2f}. '
                    'Registre uma transferência para essa conta antes de continuar, ou deixe a data de pagamento em branco '
                    'para lançar como pendente em Contas a Pagar.',
                    'erro'
                )
                return redirect(url_for('saidas'))

        nova_saida = Saida(tipo=tipo, forma_pagamento=forma_pagamento, valor=valor, descricao=descricao, data=data_saida, data_pagamento=data_pagamento, data_vencimento=data_vencimento, conta_id=conta_id)
        db.session.add(nova_saida)
        db.session.flush()
        arquivo = request.files.get('comprovante')
        nome_arquivo = salvar_comprovante(arquivo, 'saidas', nova_saida.id_saida)
        if arquivo and arquivo.filename and not nome_arquivo:
            flash('Tipo de arquivo não permitido para comprovante. Use PNG, JPG, WEBP ou PDF.', 'aviso')
        elif nome_arquivo:
            nova_saida.comprovante_arquivo = nome_arquivo
        registrar_historico('saidas', nova_saida.id_saida, 'criar', depois=snapshot_registro(nova_saida))
        db.session.commit()
        flash('Saída registrada com sucesso!', 'sucesso')
        return redirect(url_for('saidas'))
    # Filtro por período (mês/ano atual por padrão), pelo mesmo motivo de entradas():
    # sem isso, a listagem só cresce e fica pesada de rolar com o tempo de uso do sistema
    hoje = date.today()
    mostrar_todos = request.args.get('todos', '') == '1'
    ano_filtro = request.args.get('ano', type=int) or hoje.year
    mes_filtro = request.args.get('mes', type=int) or hoje.month

    consulta = Saida.query
    if not mostrar_todos:
        consulta = consulta.filter(func.strftime('%Y-%m', Saida.data) == f'{ano_filtro:04d}-{mes_filtro:02d}')

    pagina = request.args.get('page', 1, type=int)
    paginacao = consulta.order_by(Saida.data.desc()).paginate(page=pagina, per_page=ITENS_POR_PAGINA, error_out=False)
    todas_saidas = paginacao.items
    contas_por_id = {c.id_conta: c.nome for c in Conta.query.all()}
    for s in todas_saidas:
        setattr(s, 'categoria_nome', categoria_display(s.tipo, nome_saida_por_codigo()))
        setattr(s, 'conta_nome', contas_por_id.get(s.conta_id, '-'))

    return render_template(
        'saidas.html',
        saidas=todas_saidas,
        paginacao=paginacao,
        contas_saida=contas_saida(),
        formas_pagamento=FORMAS_PAGAMENTO_SAIDA,
        contas=Conta.query.filter_by(ativa=True).order_by(Conta.nome).all(),
        ano_filtro=ano_filtro,
        mes_filtro=mes_filtro,
        mostrar_todos=mostrar_todos,
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
        conta_id_original = entrada.conta_id
        valor_original = entrada.valor

        novo_tipo = request.form.get('categoria_id') or entrada.tipo
        nova_forma_pagamento = request.form.get('forma_pagamento', entrada.forma_pagamento)

        try:
            novo_valor = float(request.form.get('valor', entrada.valor))
        except (ValueError, TypeError):
            flash('Valor inválido.', 'erro')
            return redirect(url_for('editar_entrada', id=id))

        nova_descricao = request.form.get('descricao', '')
        nova_conta_id = request.form.get('conta_id', type=int) or entrada.conta_id
        novo_membro_id = request.form.get('membro_id', type=int)

        nova_data = entrada.data
        if request.form.get('data'):
            try:
                nova_data = datetime.strptime(request.form['data'], '%Y-%m-%d')
            except ValueError:
                flash('Formato de data inválido.', 'aviso')

        ok, erro = validar_reducao_entrada(conta_id_original, valor_original, nova_conta_id, novo_valor)
        if not ok:
            flash(erro, 'erro')
            return redirect(url_for('editar_entrada', id=id))

        entrada.tipo = novo_tipo
        entrada.forma_pagamento = nova_forma_pagamento
        entrada.valor = novo_valor
        entrada.descricao = nova_descricao
        entrada.conta_id = nova_conta_id
        entrada.membro_id = novo_membro_id
        entrada.data = nova_data

        arquivo = request.files.get('comprovante')
        nome_arquivo = salvar_comprovante(arquivo, 'entradas', entrada.id_entrada)
        if arquivo and arquivo.filename and not nome_arquivo:
            flash('Tipo de arquivo não permitido para comprovante. Use PNG, JPG, WEBP ou PDF.', 'aviso')
        elif nome_arquivo:
            entrada.comprovante_arquivo = nome_arquivo

        registrar_historico('entradas', entrada.id_entrada, 'editar', antes=antes, depois=snapshot_registro(entrada))
        db.session.commit()
        flash('Entrada atualizada com sucesso!', 'sucesso')
        return redirect(url_for('entradas'))

    return render_template(
        'editar_entrada.html',
        entrada=entrada,
        contas_receita=contas_receita(somente_ativas=False),  # inclui categoria atual mesmo se desativada depois
        formas_pagamento=FORMAS_PAGAMENTO_ENTRADA,
        contas=Conta.query.filter_by(ativa=True).order_by(Conta.nome).all(),
        membros=Membro.query.filter_by(ativo=True).order_by(Membro.nome).all(),
    )

@app.route('/excluir-entrada/<int:id>', methods=['POST'])
@roles_required(*CARGOS_FINANCEIRO)
def excluir_entrada(id):
    entrada = Entrada.query.get_or_404(id)

    ok, erro = validar_reducao_entrada(entrada.conta_id, entrada.valor)
    if not ok:
        flash(erro, 'erro')
        return redirect(url_for('entradas'))

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
        conta_id_original = saida.conta_id
        valor_original = saida.valor
        estava_pago = saida.data_pagamento is not None

        novo_tipo = request.form.get('tipo', saida.tipo)
        nova_forma_pagamento = request.form.get('forma_pagamento', saida.forma_pagamento)

        try:
            novo_valor = float(request.form.get('valor', saida.valor))
        except (ValueError, TypeError):
            flash('Valor inválido.', 'erro')
            return redirect(url_for('editar_saida', id=id))

        nova_descricao = request.form.get('descricao', '')

        nova_data = saida.data
        if request.form.get('data'):
            try:
                nova_data = datetime.strptime(request.form['data'], '%Y-%m-%d')
            except ValueError:
                flash('Formato de data inválido.', 'aviso')

        # Capturar datas de pagamento e vencimento
        if request.form.get('data_pagamento'):
            try:
                nova_data_pagamento = datetime.strptime(request.form['data_pagamento'], '%Y-%m-%d')
            except ValueError:
                flash('Formato de data de pagamento inválido.', 'aviso')
                nova_data_pagamento = saida.data_pagamento
        else:
            nova_data_pagamento = None

        if request.form.get('data_vencimento'):
            try:
                nova_data_vencimento = datetime.strptime(request.form['data_vencimento'], '%Y-%m-%d')
            except ValueError:
                flash('Formato de data de vencimento inválido.', 'aviso')
                nova_data_vencimento = saida.data_vencimento
        else:
            nova_data_vencimento = None

        nova_conta_id = request.form.get('conta_id', type=int) or saida.conta_id

        if nova_data_pagamento:
            ok, saldo_disponivel, conta = saldo_suficiente_para_saida(
                nova_conta_id, novo_valor,
                conta_id_original=conta_id_original, valor_original=valor_original, estava_pago=estava_pago,
            )
            if not ok:
                nome_conta = conta.nome if conta else 'selecionada'
                flash(
                    f'Saldo insuficiente em "{nome_conta}" (disponível: R$ {saldo_disponivel:.2f}) para um pagamento de R$ {novo_valor:.2f}. '
                    'Registre uma transferência para essa conta antes de continuar.',
                    'erro'
                )
                return redirect(url_for('editar_saida', id=id))

        saida.tipo = novo_tipo
        saida.forma_pagamento = nova_forma_pagamento
        saida.valor = novo_valor
        saida.descricao = nova_descricao
        saida.data = nova_data
        saida.data_pagamento = nova_data_pagamento
        saida.data_vencimento = nova_data_vencimento
        saida.conta_id = nova_conta_id

        arquivo = request.files.get('comprovante')
        nome_arquivo = salvar_comprovante(arquivo, 'saidas', saida.id_saida)
        if arquivo and arquivo.filename and not nome_arquivo:
            flash('Tipo de arquivo não permitido para comprovante. Use PNG, JPG, WEBP ou PDF.', 'aviso')
        elif nome_arquivo:
            saida.comprovante_arquivo = nome_arquivo

        registrar_historico('saidas', saida.id_saida, 'editar', antes=antes, depois=snapshot_registro(saida))
        db.session.commit()
        flash('Saída atualizada com sucesso!', 'sucesso')
        return redirect(url_for('saidas'))

    return render_template(
        'editar_saida.html',
        saida=saida,
        contas_saida=contas_saida(somente_ativas=False),  # inclui categoria atual mesmo se desativada depois
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


@app.route('/comprovante/<tabela>/<int:id>', methods=['GET'])
@roles_required(*CARGOS_DETALHE)
def ver_comprovante(tabela, id):
    if tabela == 'entradas':
        registro = Entrada.query.get_or_404(id)
    elif tabela == 'saidas':
        registro = Saida.query.get_or_404(id)
    else:
        abort(404)
    if not registro.comprovante_arquivo:
        abort(404)
    pasta = os.path.join(app.instance_path, 'comprovantes')
    return send_from_directory(pasta, registro.comprovante_arquivo)

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

    ok, saldo_disponivel, conta = saldo_suficiente_para_saida(saida.conta_id, saida.valor)
    if not ok:
        nome_conta = conta.nome if conta else 'vinculada a esta saída'
        flash(
            f'Saldo insuficiente em "{nome_conta}" (disponível: R$ {saldo_disponivel:.2f}) para pagar R$ {saida.valor:.2f}. '
            'Registre uma transferência para essa conta antes de marcar como paga.',
            'erro'
        )
        return redirect(request.referrer or url_for('contas_a_pagar'))

    antes = snapshot_registro(saida)
    saida.data_pagamento = datetime.now()
    registrar_historico('saidas', saida.id_saida, 'editar', antes=antes, depois=snapshot_registro(saida))
    db.session.commit()
    flash('Saída marcada como paga!', 'sucesso')
    return redirect(request.referrer or url_for('contas_a_pagar'))


@app.route('/saidas/<int:id>/desfazer-pagamento', methods=['POST'])
@roles_required(*CARGOS_FINANCEIRO)
def desfazer_pagamento_saida(id):
    saida = Saida.query.get_or_404(id)
    if not saida.data_pagamento:
        flash('Esta saída ainda não está marcada como paga.', 'erro')
        return redirect(request.referrer or url_for('saidas'))
    antes = snapshot_registro(saida)
    saida.data_pagamento = None
    registrar_historico('saidas', saida.id_saida, 'editar', antes=antes, depois=snapshot_registro(saida))
    db.session.commit()
    flash('Pagamento desfeito! A saída voltou para Contas a Pagar.', 'sucesso')
    return redirect(request.referrer or url_for('saidas'))


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

        conta_origem_check = Conta.query.get(conta_origem_id)
        saldo_origem = calcular_saldo_conta(conta_origem_check) if conta_origem_check else 0
        if not conta_origem_check or saldo_origem < valor:
            nome_conta = conta_origem_check.nome if conta_origem_check else 'de origem'
            flash(f'Saldo insuficiente em "{nome_conta}" (disponível: R$ {saldo_origem:.2f}) para transferir R$ {valor:.2f}.', 'erro')
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


@app.route('/plano-contas', methods=['GET', 'POST'])
@roles_required(*CARGOS_FINANCEIRO)
def plano_contas():
    if request.method == 'POST':
        codigo = request.form.get('codigo', '').strip()
        nome = request.form.get('nome', '').strip()
        tipo = request.form.get('tipo', '')

        if not codigo or not nome or tipo not in ('RECEITA', 'SAIDA'):
            flash('Preencha código, nome e um tipo válido.', 'erro')
            return redirect(url_for('plano_contas'))

        if PlanoConta.query.filter_by(codigo=codigo).first():
            flash('Já existe uma categoria com esse código.', 'erro')
            return redirect(url_for('plano_contas'))

        db.session.add(PlanoConta(codigo=codigo, nome=nome, tipo=tipo, ativo=True))
        db.session.commit()
        flash('Categoria criada com sucesso!', 'sucesso')
        return redirect(url_for('plano_contas'))

    receitas = PlanoConta.query.filter_by(tipo='RECEITA', ativo=True).order_by(PlanoConta.codigo).all()
    saidas = PlanoConta.query.filter_by(tipo='SAIDA', ativo=True).order_by(PlanoConta.codigo).all()
    return render_template('plano_contas.html', receitas=receitas, saidas=saidas)


@app.route('/editar-plano-conta/<int:id>', methods=['GET', 'POST'])
@roles_required(*CARGOS_FINANCEIRO)
def editar_plano_conta(id):
    categoria = PlanoConta.query.get_or_404(id)

    if request.method == 'POST':
        novo_codigo = request.form.get('codigo', '').strip()
        if novo_codigo != categoria.codigo and PlanoConta.query.filter_by(codigo=novo_codigo).first():
            flash('Já existe uma categoria com esse código.', 'erro')
            return redirect(url_for('editar_plano_conta', id=id))

        categoria.codigo = novo_codigo or categoria.codigo
        categoria.nome = request.form.get('nome', categoria.nome).strip() or categoria.nome
        db.session.commit()
        flash('Categoria atualizada com sucesso!', 'sucesso')
        return redirect(url_for('plano_contas'))

    return render_template('editar_plano_conta.html', categoria=categoria)


@app.route('/excluir-plano-conta/<int:id>', methods=['POST'])
@roles_required(*CARGOS_FINANCEIRO)
def excluir_plano_conta(id):
    categoria = PlanoConta.query.get_or_404(id)
    categoria.ativo = False
    db.session.commit()
    flash('Categoria removida com sucesso!', 'sucesso')
    return redirect(url_for('plano_contas'))


ESTADO_CIVIL_OPCOES = ('Solteiro(a)', 'Casado(a)', 'Divorciado(a)', 'Viúvo(a)', 'União Estável')


def parse_data_opcional(valor_str):
    """Converte uma string 'YYYY-MM-DD' em datetime, ou None se vazia/inválida."""
    if not valor_str:
        return None
    try:
        return datetime.strptime(valor_str, '%Y-%m-%d')
    except ValueError:
        return None


@app.route('/membros', methods=['GET', 'POST'])
@roles_required(*CARGOS_DETALHE)
def membros():
    if request.method == 'POST':
        nome = request.form.get('nome', '').strip()
        if not nome:
            flash('Nome do membro é obrigatório.', 'erro')
            return redirect(url_for('membros'))

        # Cadastro rápido: só o essencial. A ficha completa (dados pessoais, endereço
        # e ministeriais) é preenchida depois em Editar.
        novo_membro = Membro(
            nome=nome,
            cpf=request.form.get('cpf', '').strip(),
            email=request.form.get('email', '').strip(),
            telefone=request.form.get('telefone', '').strip(),
        )
        db.session.add(novo_membro)
        db.session.commit()
        flash('Membro cadastrado com sucesso! Complete a ficha em "Editar".', 'sucesso')
        return redirect(url_for('membros'))

    todos_membros = Membro.query.filter_by(ativo=True).order_by(Membro.nome).all()
    return render_template('membros.html', membros=todos_membros)


@app.route('/editar-membro/<int:id>', methods=['GET', 'POST'])
@roles_required(*CARGOS_DETALHE)
def editar_membro(id):
    membro = Membro.query.get_or_404(id)

    if request.method == 'POST':
        membro.nome = request.form.get('nome', membro.nome).strip() or membro.nome
        membro.cpf = request.form.get('cpf', '').strip()
        membro.email = request.form.get('email', '').strip()
        membro.telefone = request.form.get('telefone', '').strip()

        # Dados pessoais
        membro.data_nascimento = parse_data_opcional(request.form.get('data_nascimento'))
        membro.rg = request.form.get('rg', '').strip()
        membro.estado_civil = request.form.get('estado_civil', '').strip()
        membro.nome_conjuge = request.form.get('nome_conjuge', '').strip()
        membro.qtd_filhos = request.form.get('qtd_filhos', type=int) or 0
        membro.nomes_filhos = request.form.get('nomes_filhos', '').strip()
        membro.trabalha_atualmente = request.form.get('trabalha_atualmente') == 'sim'

        # Endereço
        membro.cep = request.form.get('cep', '').strip()
        membro.endereco = request.form.get('endereco', '').strip()

        # Dados ministeriais
        membro.data_batismo = parse_data_opcional(request.form.get('data_batismo'))
        membro.data_entrada_ministerio = parse_data_opcional(request.form.get('data_entrada_ministerio'))
        membro.data_saida_ministerio = parse_data_opcional(request.form.get('data_saida_ministerio'))
        membro.funcao_ministerial = request.form.get('funcao_ministerial', '').strip()

        arquivo = request.files.get('foto')
        nome_arquivo = salvar_foto_membro(arquivo, membro.id_membro)
        if arquivo and arquivo.filename and not nome_arquivo:
            flash('Tipo de arquivo não permitido para foto. Use PNG, JPG ou WEBP.', 'aviso')
        elif nome_arquivo:
            membro.foto_arquivo = nome_arquivo

        db.session.commit()
        flash('Membro atualizado com sucesso!', 'sucesso')
        return redirect(url_for('membros'))

    return render_template('editar_membro.html', membro=membro, estados_civis=ESTADO_CIVIL_OPCOES)


@app.route('/excluir-membro/<int:id>', methods=['POST'])
@roles_required(*CARGOS_DETALHE)
def excluir_membro(id):
    membro = Membro.query.get_or_404(id)
    membro.ativo = False
    db.session.commit()
    flash('Membro removido com sucesso!', 'sucesso')
    return redirect(url_for('membros'))


@app.route('/foto-membro/<int:id>', methods=['GET'])
@login_required
def ver_foto_membro(id):
    cargo_atual = session.get('cargo')
    e_o_proprio_membro = cargo_atual == 'membro' and session.get('membro_id') == id
    if cargo_atual not in CARGOS_DETALHE and not e_o_proprio_membro:
        abort(403)

    membro = Membro.query.get_or_404(id)
    if not membro.foto_arquivo:
        abort(404)
    pasta = os.path.join(app.instance_path, 'fotos_membros')
    return send_from_directory(pasta, membro.foto_arquivo)


def _dados_recibo_membro(membro_id, ano):
    """Monta os dados do recibo de doação de um membro em um dado ano. Compartilhado
    entre a rota de recibo usada pela equipe (por id) e o portal do próprio membro
    (que só pode ver o seu próprio membro_id, nunca um id arbitrário)."""
    membro = Membro.query.get_or_404(membro_id)

    entradas_membro = Entrada.query.filter(
        Entrada.membro_id == membro_id,
        func.strftime("%Y", Entrada.data) == str(ano)
    ).order_by(Entrada.data).all()

    for e in entradas_membro:
        setattr(e, 'categoria_nome', categoria_display(e.tipo, nome_receita_por_codigo()))

    total_ano = sum(e.valor or 0 for e in entradas_membro)

    return dict(
        membro=membro,
        ano=ano,
        entradas=entradas_membro,
        total_ano=total_ano,
        igreja_nome=IGREJA_NOME,
        igreja_cnpj=IGREJA_CNPJ,
        igreja_endereco=IGREJA_ENDERECO,
        igreja_cidade_uf=IGREJA_CIDADE_UF,
        data_emissao=date.today(),
    )


@app.route('/membros/<int:id>/recibo', methods=['GET'])
@roles_required(*CARGOS_DETALHE)
def recibo_membro(id):
    ano = request.args.get('ano', type=int) or date.today().year
    return render_template('recibo_membro.html', **_dados_recibo_membro(id, ano))


@app.route('/meu-portal', methods=['GET'])
@roles_required('membro')
def meu_portal():
    membro_id = session.get('membro_id')
    if not membro_id:
        flash('Sua conta não está vinculada a um registro de membro. Contate um administrador.', 'erro')
        return redirect(url_for('logout'))

    ano = request.args.get('ano', type=int) or date.today().year
    dados = _dados_recibo_membro(membro_id, ano)

    anos_disponiveis = sorted({
        e.data.year for e in Entrada.query.filter_by(membro_id=membro_id).all()
    }, reverse=True) or [date.today().year]

    return render_template('meu_portal.html', anos_disponiveis=anos_disponiveis, **dados)


@app.route('/meu-portal/recibo', methods=['GET'])
@roles_required('membro')
def meu_portal_recibo():
    membro_id = session.get('membro_id')
    if not membro_id:
        abort(403)
    ano = request.args.get('ano', type=int) or date.today().year
    return render_template('recibo_membro.html', **_dados_recibo_membro(membro_id, ano))


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

        if not nome or not email or not senha or cargo not in CARGOS_STAFF:
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

    todos_usuarios = Usuario.query.filter_by(pendente_aprovacao=False).order_by(Usuario.nome).all()
    pendentes = Usuario.query.filter_by(pendente_aprovacao=True).order_by(Usuario.data_criacao.desc()).all()

    pasta_backup = os.path.join(app.instance_path, 'backups')
    backups = sorted(glob.glob(os.path.join(pasta_backup, 'igreja_finance_*.db')), reverse=True)
    total_backups = len(backups)
    ultimo_backup = None
    if backups:
        nome = os.path.basename(backups[0])
        try:
            ts = nome.replace('igreja_finance_', '').replace('.db', '')
            ultimo_backup = datetime.strptime(ts, '%Y%m%d_%H%M%S')
        except ValueError:
            ultimo_backup = None

    ultimo_aviso = None
    caminho_aviso = _caminho_ultimo_aviso()
    if os.path.exists(caminho_aviso):
        with open(caminho_aviso, 'r', encoding='utf-8') as f:
            ultimo_aviso = f.read().strip() or None

    return render_template(
        'usuarios.html',
        usuarios=todos_usuarios,
        pendentes=pendentes,
        cargos=CARGOS_VALIDOS,
        total_backups=total_backups,
        ultimo_backup=ultimo_backup,
        smtp_configurado=smtp_configurado(),
        smtp_destinatario=SMTP_DESTINATARIO,
        ultimo_aviso=ultimo_aviso,
    )


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
        if cargo_novo not in CARGOS_VALIDOS:
            flash('Cargo inválido.', 'erro')
            return redirect(url_for('editar_usuario', id=id))
        if cargo_novo == 'membro' and usuario.cargo != 'membro':
            flash('O cargo "Membro" só pode ser atribuído pelo próprio cadastro em /cadastro, pois exige vínculo com um registro de Membro.', 'erro')
            return redirect(url_for('editar_usuario', id=id))
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


@app.route('/aprovar-usuario/<int:id>', methods=['POST'])
@roles_required('administrador')
def aprovar_usuario(id):
    usuario = Usuario.query.get_or_404(id)
    if not usuario.pendente_aprovacao:
        flash('Este cadastro não está pendente de aprovação.', 'erro')
        return redirect(url_for('usuarios'))
    usuario.ativo = True
    usuario.pendente_aprovacao = False
    db.session.commit()
    flash(f'Cadastro de {usuario.nome} aprovado com sucesso!', 'sucesso')
    return redirect(url_for('usuarios'))


@app.route('/rejeitar-usuario/<int:id>', methods=['POST'])
@roles_required('administrador')
def rejeitar_usuario(id):
    usuario = Usuario.query.get_or_404(id)
    if not usuario.pendente_aprovacao:
        flash('Este cadastro não está pendente de aprovação.', 'erro')
        return redirect(url_for('usuarios'))

    # Se o cadastro criou um Membro novo (não vinculou a um já existente) e ele nunca
    # recebeu nenhuma doação, remove junto — senão fica um registro "fantasma" na
    # lista de Membros da equipe, sem login e sem relação real com a igreja.
    if usuario.membro_id:
        membro = Membro.query.get(usuario.membro_id)
        if membro and membro.criado_via_cadastro and not Entrada.query.filter_by(membro_id=membro.id_membro).first():
            db.session.delete(membro)

    db.session.delete(usuario)
    db.session.commit()
    flash('Cadastro rejeitado e removido.', 'sucesso')
    return redirect(url_for('usuarios'))


@app.route('/backup-agora', methods=['POST'])
@roles_required('administrador')
def backup_agora():
    destino = criar_backup_banco()
    if destino:
        flash(f'Backup criado com sucesso: {os.path.basename(destino)}', 'sucesso')
    else:
        flash('Não foi possível criar o backup (banco de dados não encontrado).', 'erro')
    return redirect(url_for('usuarios'))


@app.route('/enviar-aviso-vencimento-agora', methods=['POST'])
@roles_required('administrador')
def enviar_aviso_vencimento_agora():
    resultado = verificar_e_enviar_avisos_vencimento(forcar=True)
    if resultado['enviado']:
        flash(resultado['motivo'], 'sucesso')
    else:
        flash(resultado['motivo'], 'aviso')
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


@app.route('/log-acessos', methods=['GET'])
@roles_required('administrador')
def log_acessos():
    filtro = request.args.get('status', '')
    query = LoginLog.query.order_by(LoginLog.data_hora.desc())
    if filtro == 'sucesso':
        query = query.filter_by(sucesso=True)
    elif filtro == 'falha':
        query = query.filter_by(sucesso=False)
    logs = query.limit(200).all()
    return render_template('log_acessos.html', logs=logs, filtro=filtro)


@app.route('/relatorio-anual', methods=['GET'])
@roles_required(*CARGOS_DETALHE)
def relatorio_anual():
    ano = request.args.get('ano', type=int) or date.today().year
    resumo = get_resumo_anual(ano)
    return render_template('relatorio_anual.html', ano=ano, resumo=resumo)


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
            'categoria': categoria_display(e.tipo, nome_receita_por_codigo()),
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
            'categoria': categoria_display(s.tipo, nome_saida_por_codigo()),
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
        catEntrada=contas_receita(somente_ativas=False),  # filtro deve alcançar categorias antigas também
        catSaida=contas_saida(somente_ativas=False),
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
    if session.get('cargo') == 'membro':
        return jsonify({"error": "Acesso não permitido"}), 403

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
@csrf.exempt  # chamado via fetch() JS, não formulário; não altera dados financeiros
def api_chat():
    """Recebe a pergunta do usuário, monta o contexto financeiro e chama a Gemini
    inteiramente no servidor (a chave de API nunca é exposta ao navegador)."""
    if 'usuario' not in session:
        return jsonify({"error": "Não autenticado"}), 401
    if session.get('cargo') == 'membro':
        return jsonify({"error": "Acesso não permitido"}), 403

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
CARGOS_VALIDOS = ('administrador', 'tesoureiro', 'secretario', 'pastor', 'consulta', 'membro')
CARGOS_STAFF = tuple(c for c in CARGOS_VALIDOS if c != 'membro')  # cargo 'membro' só é criado via /cadastro (precisa de membro_id)
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


COMPROVANTE_EXTENSOES_PERMITIDAS = {'.png', '.jpg', '.jpeg', '.pdf', '.webp'}


def salvar_comprovante(arquivo, tabela, id_registro):
    """Salva o arquivo de comprovante enviado em instance/comprovantes/ e retorna
    o nome do arquivo salvo, ou None se nenhum arquivo válido foi enviado."""
    if not arquivo or not arquivo.filename:
        return None
    ext = os.path.splitext(arquivo.filename)[1].lower()
    if ext not in COMPROVANTE_EXTENSOES_PERMITIDAS:
        return None
    pasta = os.path.join(app.instance_path, 'comprovantes')
    os.makedirs(pasta, exist_ok=True)
    nome_arquivo = f"{tabela}_{id_registro}_{secrets.token_hex(6)}{ext}"
    arquivo.save(os.path.join(pasta, nome_arquivo))
    return nome_arquivo


FOTO_EXTENSOES_PERMITIDAS = {'.png', '.jpg', '.jpeg', '.webp'}


def salvar_foto_membro(arquivo, membro_id):
    """Salva a foto do membro enviada em instance/fotos_membros/ e retorna o nome
    do arquivo salvo, ou None se nenhum arquivo válido foi enviado."""
    if not arquivo or not arquivo.filename:
        return None
    ext = os.path.splitext(arquivo.filename)[1].lower()
    if ext not in FOTO_EXTENSOES_PERMITIDAS:
        return None
    pasta = os.path.join(app.instance_path, 'fotos_membros')
    os.makedirs(pasta, exist_ok=True)
    nome_arquivo = f"membro_{membro_id}_{secrets.token_hex(6)}{ext}"
    arquivo.save(os.path.join(pasta, nome_arquivo))
    return nome_arquivo


MAX_BACKUPS = 30


def criar_backup_banco():
    """Copia o arquivo do banco para instance/backups/ com timestamp, mantendo
    só os MAX_BACKUPS mais recentes. Retorna o caminho do backup criado, ou None
    se o banco ainda não existir (primeira execução)."""
    origem = os.path.join(app.instance_path, 'igreja_finance.db')
    if not os.path.exists(origem):
        return None

    pasta_backup = os.path.join(app.instance_path, 'backups')
    os.makedirs(pasta_backup, exist_ok=True)

    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    destino = os.path.join(pasta_backup, f'igreja_finance_{timestamp}.db')
    shutil.copy2(origem, destino)

    backups = sorted(glob.glob(os.path.join(pasta_backup, 'igreja_finance_*.db')))
    for antigo in backups[:-MAX_BACKUPS]:
        os.remove(antigo)

    return destino


with app.app_context():
    _backup_inicial = criar_backup_banco()
    if _backup_inicial:
        print(f"Backup criado: {_backup_inicial}")

    db.create_all()

    # Migração: novas colunas em entradas/saidas para vínculo com conta e membro
    _add_column_if_missing('entradas', 'conta_id', 'INTEGER')
    _add_column_if_missing('entradas', 'membro_id', 'INTEGER')
    _add_column_if_missing('saidas', 'conta_id', 'INTEGER')
    _add_column_if_missing('usuarios', 'ativo', 'BOOLEAN DEFAULT 1')
    _add_column_if_missing('usuarios', 'membro_id', 'INTEGER')
    _add_column_if_missing('usuarios', 'pendente_aprovacao', 'BOOLEAN DEFAULT 0')
    _add_column_if_missing('membros', 'criado_via_cadastro', 'BOOLEAN DEFAULT 0')
    _add_column_if_missing('membros', 'foto_arquivo', 'TEXT')
    _add_column_if_missing('membros', 'data_nascimento', 'DATETIME')
    _add_column_if_missing('membros', 'rg', 'TEXT')
    _add_column_if_missing('membros', 'estado_civil', 'TEXT')
    _add_column_if_missing('membros', 'nome_conjuge', 'TEXT')
    _add_column_if_missing('membros', 'qtd_filhos', 'INTEGER DEFAULT 0')
    _add_column_if_missing('membros', 'nomes_filhos', 'TEXT')
    _add_column_if_missing('membros', 'trabalha_atualmente', 'BOOLEAN DEFAULT 0')
    _add_column_if_missing('membros', 'cep', 'TEXT')
    _add_column_if_missing('membros', 'endereco', 'TEXT')
    _add_column_if_missing('membros', 'data_batismo', 'DATETIME')
    _add_column_if_missing('membros', 'data_entrada_ministerio', 'DATETIME')
    _add_column_if_missing('membros', 'data_saida_ministerio', 'DATETIME')
    _add_column_if_missing('membros', 'funcao_ministerial', 'TEXT')
    _add_column_if_missing('entradas', 'comprovante_arquivo', 'TEXT')
    _add_column_if_missing('saidas', 'comprovante_arquivo', 'TEXT')

    # Garante ao menos uma conta padrão e migra lançamentos antigos sem conta definida
    if not Conta.query.first():
        db.session.add(Conta(nome='Caixa', tipo='caixa', saldo_inicial=0.0, ativa=True))
        db.session.commit()
        print("Conta padrão 'Caixa' criada.")

    conta_padrao = Conta.query.order_by(Conta.id_conta).first()
    Entrada.query.filter_by(conta_id=None).update({Entrada.conta_id: conta_padrao.id_conta})
    Saida.query.filter_by(conta_id=None).update({Saida.conta_id: conta_padrao.id_conta})
    db.session.commit()

    # Popula o plano de contas com as categorias padrão, só na primeira execução
    if not PlanoConta.query.first():
        for c in SEED_PLANO_RECEITA:
            db.session.add(PlanoConta(codigo=c['codigo'], nome=c['nome'], tipo='RECEITA', ativo=True))
        for c in SEED_PLANO_SAIDA:
            db.session.add(PlanoConta(codigo=c['codigo'], nome=c['nome'], tipo='SAIDA', ativo=True))
        db.session.commit()
        print("Plano de contas inicial criado.")

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

    if smtp_configurado():
        threading.Thread(target=_loop_avisos_vencimento, daemon=True).start()
        print("Aviso de vencimento por e-mail: ativado (verificação a cada hora).")
    else:
        print("Aviso de vencimento por e-mail: desativado (defina SMTP_HOST, SMTP_USER, "
              "SMTP_SENHA e SMTP_DESTINATARIO no ambiente para ativar).")

    if debug_mode:
        app.run(host='0.0.0.0', port=port, debug=True)
    else:
        from waitress import serve
        print(f"Servindo com Waitress (produção) em http://0.0.0.0:{port}")
        serve(app, host='0.0.0.0', port=port)
