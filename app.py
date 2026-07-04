from flask import Flask, render_template, request, redirect, url_for, session, flash, jsonify, make_response
import csv
import io
import os
import requests
from flask_sqlalchemy import SQLAlchemy
from flask_bcrypt import Bcrypt
from datetime import datetime, date
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


class Entrada(db.Model):
    __tablename__ = "entradas"
    id_entrada = db.Column(db.Integer, primary_key=True)
    tipo = db.Column(db.String(50)) # Corresponde à categoria (Dízimo, Oferta, etc.)
    forma_pagamento = db.Column(db.String(50))
    valor = db.Column(db.Float)
    data = db.Column(db.DateTime, default=datetime.now)
    descricao = db.Column(db.String(200))


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


# ==============================
# FUNÇÕES AUXILIARES DE DADOS E RELATÓRIO
# ==============================

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
        if usuario and bcrypt.check_password_hash(usuario.senha, senha):
            session['usuario'] = usuario.nome
            session['cargo'] = usuario.cargo
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

    return render_template(
        'dashboard.html',
        usuario=session['usuario'],
        hoje=hoje,
        total_entradas=float(summary['total_entradas']),
        total_saidas=float(summary['total_saidas']),
        saldo=float(summary['saldo_atual']),
        total_dia=float(summary['total_entradas_hoje']),
    )

@app.route('/entradas', methods=['GET', 'POST'])
@login_required
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

        nova_entrada = Entrada(
            tipo=tipo,
            forma_pagamento=forma_pagamento,
            valor=valor,
            descricao=descricao,
            data=data_entrada
        )
        db.session.add(nova_entrada)
        db.session.commit()
        flash('Entrada registrada com sucesso!', 'sucesso')
        return redirect(url_for('entradas'))

    # 🔹 Carrega todas as entradas
    todas_entradas = Entrada.query.order_by(Entrada.data.desc()).all()

    # Anexa atributos temporários aos objetos de entrada para uso no template
    for e in todas_entradas:
        codigo = (e.tipo or '').strip()
        setattr(e, 'categoria_codigo', codigo)
        setattr(e, 'categoria_nome', NOME_RECEITA_POR_CODIGO.get(codigo, codigo))

    return render_template(
        'entradas.html',
        entradas=todas_entradas,
        contas_receita=CONTAS_RECEITA,
        formas_pagamento=FORMAS_PAGAMENTO_ENTRADA,
        pode_editar=pode_editar_financeiro(),
    )


@app.route('/saidas', methods=['GET', 'POST'])
@login_required
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

        nova_saida = Saida(tipo=tipo, forma_pagamento=forma_pagamento, valor=valor, descricao=descricao, data=data_saida, data_pagamento=data_pagamento, data_vencimento=data_vencimento)
        db.session.add(nova_saida)
        db.session.commit()
        flash('Saída registrada com sucesso!', 'sucesso')
        return redirect(url_for('saidas'))
    todas_saidas = Saida.query.order_by(Saida.data.desc()).all()
    for s in todas_saidas:
        setattr(s, 'categoria_nome', categoria_display(s.tipo, NOME_SAIDA_POR_CODIGO))

    return render_template(
        'saidas.html',
        saidas=todas_saidas,
        contas_saida=CONTAS_SAIDA,
        formas_pagamento=FORMAS_PAGAMENTO_SAIDA,
        pode_editar=pode_editar_financeiro(),
    )

# ==============================
# ROTAS DE EDIÇÃO E EXCLUSÃO
# ==============================

@app.route('/editar-entrada/<int:id>', methods=['GET', 'POST'])
@roles_required(*CARGOS_FINANCEIRO)
def editar_entrada(id):
    entrada = Entrada.query.get_or_404(id)
    
    if request.method == 'POST':
        entrada.tipo = request.form.get('categoria_id') or entrada.tipo
        entrada.forma_pagamento = request.form.get('forma_pagamento', entrada.forma_pagamento)
        
        try:
            entrada.valor = float(request.form.get('valor', entrada.valor))
        except (ValueError, TypeError):
            flash('Valor inválido.', 'erro')
            return redirect(url_for('editar_entrada', id=id))
        
        entrada.descricao = request.form.get('descricao', '')
        
        if request.form.get('data'):
            try:
                entrada.data = datetime.strptime(request.form['data'], '%Y-%m-%d')
            except ValueError:
                flash('Formato de data inválido.', 'aviso')
        
        db.session.commit()
        flash('Entrada atualizada com sucesso!', 'sucesso')
        return redirect(url_for('entradas'))
    
    return render_template(
        'editar_entrada.html',
        entrada=entrada,
        contas_receita=CONTAS_RECEITA,
        formas_pagamento=FORMAS_PAGAMENTO_ENTRADA,
    )

@app.route('/excluir-entrada/<int:id>', methods=['POST'])
@roles_required(*CARGOS_FINANCEIRO)
def excluir_entrada(id):
    entrada = Entrada.query.get_or_404(id)
    db.session.delete(entrada)
    db.session.commit()
    flash('Entrada excluída com sucesso!', 'sucesso')
    return redirect(url_for('entradas'))

@app.route('/editar-saida/<int:id>', methods=['GET', 'POST'])
@roles_required(*CARGOS_FINANCEIRO)
def editar_saida(id):
    saida = Saida.query.get_or_404(id)
    
    if request.method == 'POST':
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
        
        db.session.commit()
        flash('Saída atualizada com sucesso!', 'sucesso')
        return redirect(url_for('saidas'))
    
    return render_template(
        'editar_saida.html',
        saida=saida,
        contas_saida=CONTAS_SAIDA,
        formas_pagamento=FORMAS_PAGAMENTO_SAIDA,
    )

@app.route('/excluir-saida/<int:id>', methods=['POST'])
@roles_required(*CARGOS_FINANCEIRO)
def excluir_saida(id):
    saida = Saida.query.get_or_404(id)
    db.session.delete(saida)
    db.session.commit()
    flash('Saída excluída com sucesso!', 'sucesso')
    return redirect(url_for('saidas'))

@app.route('/relatorio', methods=['GET'])
@login_required
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
@login_required
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
@login_required
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
CARGOS_VALIDOS = ('administrador', 'tesoureiro', 'secretario', 'pastor')
MIGRACAO_CARGOS_LEGADO = {'admin': 'administrador'}

with app.app_context():
    db.create_all()

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
    app.run(host='0.0.0.0', port=port, debug=debug_mode)
