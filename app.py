from flask import Flask, render_template, request, redirect, url_for, session, g, jsonify
import sqlite3
from groq import Groq
from datetime import datetime, timedelta
import re
import json
import math
import os
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)
app.secret_key = 'chave_secreta_super_segura'

DATABASE = 'usuarios.db'

def get_db():
    db = getattr(g, '_database', None)
    if db is None:
        db = g._database = sqlite3.connect(DATABASE)
        db.row_factory = sqlite3.Row
    return db

@app.teardown_appcontext
def close_connection(exception):
    db = getattr(g, '_database', None)
    if db is not None:
        db.close()

def init_db():
    db = sqlite3.connect(DATABASE)
    cursor = db.cursor()
    
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS usuarios (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT UNIQUE NOT NULL,
            senha TEXT NOT NULL,
            nome TEXT NOT NULL,
            sobrenome TEXT NOT NULL,
            cpf TEXT UNIQUE NOT NULL,
            telefone TEXT NOT NULL,
            tipo TEXT NOT NULL, 
            personal_vinculado TEXT,
            limitacoes TEXT
        )
    """)
    
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS treinos_gerados (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            usuario_email TEXT NOT NULL,
            treino_texto TEXT NOT NULL,
            data_criacao TEXT NOT NULL,
            data_validade TEXT NOT NULL,
            status TEXT DEFAULT 'pendente',
            nivel TEXT,
            foco TEXT,
            objetivo TEXT,
            FOREIGN KEY(usuario_email) REFERENCES usuarios(email)
        )
    """)
    
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS historico_treino (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            usuario_email TEXT NOT NULL,
            tipo_treino TEXT NOT NULL,
            cargas_anotadas TEXT NOT NULL,
            data_execucao TEXT NOT NULL,
            FOREIGN KEY(usuario_email) REFERENCES usuarios(email)
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS agenda_disponivel (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            personal_email TEXT NOT NULL,
            data TEXT NOT NULL,        
            horario TEXT NOT NULL,     
            vagas_totais INTEGER NOT NULL
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS checkins_aulas (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            agenda_id INTEGER NOT NULL,
            aluno_email TEXT NOT NULL,
            status TEXT DEFAULT 'Pendente', 
            FOREIGN KEY(agenda_id) REFERENCES agenda_disponivel(id),
            FOREIGN KEY(aluno_email) REFERENCES usuarios(email)
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS historico_progresso (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            usuario_email TEXT NOT NULL,
            peso REAL NOT NULL,
            altura REAL NOT NULL,
            imc REAL NOT NULL,
            status_imc TEXT NOT NULL,
            braco_esq REAL,
            braco_dir REAL,
            cintura REAL,
            torax REAL,
            quadril REAL,
            dobra_torax REAL DEFAULT 0,
            dobra_abdomen REAL DEFAULT 0,
            dobra_coxa REAL DEFAULT 0,
            dobra_triceps REAL DEFAULT 0,
            dobra_suprailiaca REAL DEFAULT 0,
            percentual_gordura REAL DEFAULT 0,
            massa_magra REAL DEFAULT 0,
            data_registro TEXT NOT NULL,
            FOREIGN KEY(usuario_email) REFERENCES usuarios(email)
        )
    """)
    
    try:
        cursor.execute("ALTER TABLE usuarios ADD COLUMN limitacoes TEXT")
    except sqlite3.OperationalError: pass
    
    try:
        cursor.execute("""
            INSERT INTO usuarios (email, senha, nome, sobrenome, cpf, telefone, tipo)
            VALUES ('admin@admin.com', 'admin123', 'Administrador', 'Master', '000.000.000-00', '000000000', 'admin')
        """)
    except sqlite3.IntegrityError: pass

    try:
        cursor.execute("""
            INSERT INTO usuarios (email, senha, nome, sobrenome, cpf, telefone, tipo)
            VALUES ('lukas.atademos@gmail.com', '123', 'Lukas', 'Atademos', '111.111.111-11', '999999999', 'personal')
        """)
    except sqlite3.IntegrityError: pass

    try:
        cursor.execute("""
            INSERT INTO usuarios (email, senha, nome, sobrenome, cpf, telefone, tipo, personal_vinculado)
            VALUES ('giovanelli.contato7@gmail.com', '123', 'Giovanelli', 'Contato', '222.222.222-22', '888888888', 'aluno', 'lukas.atademos@gmail.com')
        """)
    except sqlite3.IntegrityError: pass
    
    db.commit()
    db.close()

init_db()

def extrair_blocos_treino(texto_completo):
    treinos = {'A': '', 'B': '', 'C': '', 'D': '', 'E': '', 'F': ''}
    matches = re.findall(r"MARCADOR_TREINO_([A-F])\s*([\s\S]*?)(?=MARCADOR_TREINO_[A-F]|$)", texto_completo)
    if matches:
        for letra, conteudo in matches: treinos[letra] = conteudo.strip()
        return treinos
    matches_livres = re.findall(r"(?:TREINO|BLOCO|FICHA)\s*([A-F])[:\-\s\n]*([\s\S]*?)(?=(?:TREINO|BLOCO|FICHA)\s*[A-F]|$)", texto_completo, re.IGNORECASE)
    if matches_livres:
        for letra, conteudo in matches_livres: treinos[letra.upper()] = conteudo.strip()
        return treinos
    treinos['A'] = texto_completo.strip()
    return treinos

def determinar_status_imc(imc):
    if imc < 18.5: return "Abaixo do peso (Ideal: 18.5 a 24.9)"
    elif imc < 25: return "Peso normal (Parabéns!)"
    elif imc < 30: return "Sobrepeso (Ideal: 18.5 a 24.9)"
    else: return "Obesidade (Ideal: 18.5 a 24.9)"

def chamar_ia_groq(objetivo, foco, nivel, frequencia_dias, limitacoes="Nenhuma"):
    divisao_muscular = (
        "Siga distribuição de grupos musculares por letra de acordo com a quantidade de dias:\n"
        "- Se for 1 dia: TREINO A (Corpo Inteiro - Full Body).\n"
        "- Se for 2 dias: TREINO A (Peito, Tríceps, Ombro) e TREINO B (Costas, Bíceps, Pernas).\n"
        "- Se for 3 dias ou mais:\n"
        "  * TREINO A: PEITO e TRÍCEPS.\n"
        "  * TREINO B: COSTAS e BÍCEPS.\n"
        "  * TREINO C: PERNAS completo.\n"
        "  * TREINO D: OMBRO e TRAPÉZIO.\n"
        "  * TREINO E: ABDÔMEN e CARDIO."
    )
    
    if str(frequencia_dias) == '5':
        letras_exigidas = "TREINO A, TREINO B, TREINO C, TREINO D e TREINO E"
        instrucao_marcadores = "MARCADOR_TREINO_A, MARCADOR_TREINO_B, MARCADOR_TREINO_C, MARCADOR_TREINO_D e MARCADOR_TREINO_E"
    elif str(frequencia_dias) == '4':
        letras_exigidas = "TREINO A, TREINO B, TREINO C e TREINO D"
        instrucao_marcadores = "MARCADOR_TREINO_A, MARCADOR_TREINO_B, MARCADOR_TREINO_C e MARCADOR_TREINO_D"
    elif str(frequencia_dias) == '3':
        letras_exigidas = "TREINO A, TREINO B e TREINO C"
        instrucao_marcadores = "MARCADOR_TREINO_A, MARCADOR_TREINO_B e MARCADOR_TREINO_C"
    elif str(frequencia_dias) == '2':
        letras_exigidas = "TREINO A e TREINO B"
        instrucao_marcadores = "MARCADOR_TREINO_A e MARCADOR_TREINO_B"
    else:
        letras_exigidas = "TREINO A"
        instrucao_marcadores = "MARCADOR_TREINO_A"

    prompt = (
        f"Monte uma ficha de musculação profissional padrão de nível {nivel.upper()} dividida exatamente em {letras_exigidas}.\n\n"
        f"Objetivo: {objetivo}.\n"
        f"Foco Informado: {foco}.\n"
        f"Restrições e observações: {limitacoes}.\n\n"
        f"REGRAS DE SEGURANÇA:\n"
        f"Se houver limitações informadas (ex: dores nas articulações), adapte a seleção de exercícios excluindo movimentos contraindicados.\n\n"
        f"REGRAS DE DIVISÃO DOS MÚSCULOS:\n"
        f"{divisao_muscular}\n\n"
        f"DIRETRIZES OBRIGATÓRIAS DE FORMATAÇÃO E ESCRITA:\n"
        f"1. Proibido incluir qualquer menção a termos como 'Inteligência Artificial', 'IA', 'modelo de linguagem', 'assistente virtual' ou 'Groq'. O texto deve parecer escrito manualmente por um especialista humano.\n"
        f"2. NÃO insira textos informativos, observações, saudações ou comentários iniciais/finais. Vá diretamente para a estrutura da ficha.\n"
        f"3. Separe cada ficha colando os identificadores exatamente assim: {instrucao_marcadores}.\n"
        f"4. Insira o respectivo 'MARCADOR_TREINO_X' colado antes de listar os exercícios daquela letra.\n"
        f"5. Cada bloco de treino deve listar de 5 a 6 exercícios reais específicos com séries e repetições adequadas ao nível {nivel.upper()}."
    )
    
    chave_segura = os.getenv("GROQ_API_KEY")
    client = Groq(api_key=chave_segura)
    return client.chat.completions.create(model="llama-3.3-70b-versatile", messages=[{"role": "user", "content": prompt}]).choices[0].message.content

# --- ROTAS DE AUTENTICAÇÃO ---

@app.route('/login', methods=['GET', 'POST'])
def login():
    erro = None
    sucesso = None
    if request.method == 'POST':
        acao = request.form.get('acao')
        email = request.form.get('email').strip().lower()
        senha = request.form.get('senha')
        
        if acao == 'cadastrar':
            nome = request.form.get('nome').strip()
            sobrenome = request.form.get('sobrenome').strip()
            cpf = request.form.get('cpf').strip()
            telefone = request.form.get('telefone').strip()
            tipo = request.form.get('tipo') 
            personal_vinculado = request.form.get('personal_vinculado', '').strip().lower()
            
            try:
                db = get_db()
                db.cursor().execute("""
                    INSERT INTO usuarios (email, senha, nome, sobrenome, cpf, telefone, tipo, personal_vinculado) 
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """, (email, senha, nome, sobrenome, cpf, telefone, tipo, personal_vinculado if tipo == 'aluno' else None))
                db.commit()
                sucesso = "Cadastro realizado com sucesso! Faça o login."
            except sqlite3.IntegrityError:
                erro = "E-mail ou CPF já cadastrados!"
                
        elif acao == 'login':
            db = get_db()
            usuario = db.cursor().execute('SELECT * FROM usuarios WHERE email = ? AND senha = ?', (email, senha)).fetchone()
            if usuario:
                session['usuario_logado'] = usuario['email']
                session['usuario_tipo'] = usuario['tipo']
                if usuario['tipo'] == 'admin': return redirect(url_for('painel_admin'))
                elif usuario['tipo'] == 'personal': return redirect(url_for('painel_personal'))
                else: return redirect(url_for('meus_treinos'))
            else: erro = "E-mail ou Senha incorretos!"
    return render_template('login.html', erro=erro, sucesso=sucesso)

# --- ROTAS DE VÍNCULO ---

@app.route('/compartilhado/desvincular/<string:email_aluno>')
def desvincular_aluno(email_aluno):
    if 'usuario_logado' not in session or session['usuario_tipo'] not in ['admin', 'personal']: return redirect(url_for('login'))
    db = get_db()
    db.cursor().execute("UPDATE usuarios SET personal_vinculado = NULL WHERE email = ?", (email_aluno,))
    db.cursor().execute("UPDATE treinos_gerados SET status = 'arquivado' WHERE usuario_email = ?", (email_aluno,))
    db.commit()
    if session['usuario_tipo'] == 'admin': return redirect(url_for('painel_admin'))
    return redirect(url_for('painel_personal'))

@app.route('/personal/criar_treino_manual/<string:email_aluno>', methods=['POST'])
def criar_treino_manual(email_aluno):
    if 'usuario_logado' not in session or session['usuario_tipo'] != 'personal': return redirect(url_for('login'))
    db = get_db()
    
    objetivo = request.form.get('objetivo')
    foco = request.form.get('foco')
    nivel = request.form.get('nivel')
    dias = request.form.get('dias', '5')
    
    perfil_aluno = db.cursor().execute("SELECT limitacoes FROM usuarios WHERE email = ?", (email_aluno,)).fetchone()
    obs_medicas = perfil_aluno['limitacoes'] if perfil_aluno and perfil_aluno['limitacoes'] else "Nenhuma"
    
    try:
        treino_gerado = chamar_ia_groq(objetivo, foco, nivel, dias, obs_medicas)
        db.cursor().execute("UPDATE treinos_gerados SET status = 'arquivado' WHERE usuario_email = ? AND status = 'pendente'", (email_aluno,))
        
        cursor = db.cursor()
        cursor.execute("""
            INSERT INTO treinos_gerados (usuario_email, treino_texto, data_criacao, data_validade, status, nivel, foco, objective)
            VALUES (?, ?, ?, ?, 'pendente', ?, ?, ?)
        """, (email_aluno, treino_gerado, datetime.now().strftime("%d/%m/%Y"), (datetime.now() + timedelta(days=60)).strftime("%d/%m/%Y"), nivel, foco, objetivo))
        
        id_novo_treino = cursor.lastrowid
        db.commit()
        return redirect(url_for('personal_revisar_treino', id_treino=id_novo_treino))
    except Exception as e: return f"Erro na renovação técnica: {e}"

# --- PAINÉIS DE GERENCIAMENTO ---

@app.route('/admin')
def painel_admin():
    if 'usuario_logado' not in session or session['usuario_tipo'] != 'admin': return redirect(url_for('login'))
    db = get_db()
    personais = db.cursor().execute('SELECT * FROM usuarios WHERE tipo = "personal" ORDER BY nome ASC').fetchall()
    estrutura_SaaS = {p: db.cursor().execute('SELECT * FROM usuarios WHERE tipo = "aluno" AND personal_vinculado = ? ORDER BY nome ASC', (p['email'],)).fetchall() for p in personais}
    alunos_orfaos = db.cursor().execute('SELECT * FROM usuarios WHERE tipo = "aluno" AND (personal_vinculado IS NULL OR personal_vinculado = "") ORDER BY nome ASC').fetchall()
    return render_template('painel_admin.html', estrutura=estrutura_SaaS, orfaos=alunos_orfaos, personais=personais)

@app.route('/personal')
def painel_personal():
    if 'usuario_logado' not in session or session['usuario_tipo'] != 'personal': return redirect(url_for('login'))
    db = get_db()
    
    alunos = db.cursor().execute("""
        SELECT u.*, t.data_validade, t.objetivo, t.foco, t.nivel 
        FROM usuarios u 
        LEFT JOIN treinos_gerados t ON u.email = t.usuario_email AND t.status = 'ativo'
        WHERE u.tipo = 'aluno' AND u.personal_vinculado = ? 
        ORDER BY u.nome ASC
    """, (session['usuario_logado'],)).fetchall()
    
    pendentes = db.cursor().execute("SELECT t.*, u.nome, u.sobrenome FROM treinos_gerados t JOIN usuarios u ON t.usuario_email = u.email WHERE t.status = 'pendente' AND u.personal_vinculado = ?", (session['usuario_logado'],)).fetchall()
    return render_template('painel_personal.html', alunos=alunos, pendentes=pendentes)

@app.route('/personal/api/ver_ficha/<email_aluno>')
def api_ver_ficha(email_aluno):
    if 'usuario_logado' not in session or session['usuario_tipo'] != 'personal': return jsonify({"erro": "Acesso negado"}), 403
    db = get_db()
    treino = db.cursor().execute("SELECT treino_texto FROM treinos_gerados WHERE usuario_email = ? AND status = 'ativo' ORDER BY id DESC", (email_aluno,)).fetchone()
    if treino:
        texto_limpo = treino['treino_texto'].replace("MARCADOR_TREINO_", "\n💪 TREINO ")
        return jsonify({"treino": texto_limpo.strip()})
    return jsonify({"treino": None})

@app.route('/compartilhado/ver_treino/<email_aluno>')
def ver_treino_compartilhado(email_aluno):
    if 'usuario_logado' not in session: return redirect(url_for('login'))
    db = get_db()
    aluno = db.cursor().execute('SELECT * FROM usuarios WHERE email = ?', (email_aluno,)).fetchone()
    if not aluno: return "Ficha não localizada.", 404
    
    treino_filtro = request.args.get('treino_filtro', 'TREINO A').upper().strip()
    historico_treinos = db.cursor().execute('SELECT * FROM historico_treino WHERE usuario_email = ? ORDER BY id ASC', (email_aluno,)).fetchall()
    datas_carga, valores_carga = [], []
    for linha in historico_treinos:
        if linha['tipo_treino'].upper().strip() == treino_filtro:
            match_peso = re.search(r'Carga:\s*(\d+)', linha['cargas_anotadas'], re.IGNORECASE)
            if not match_peso: match_peso = re.search(r'(\d+)', linha['cargas_anotadas'])
            if match_peso:
                datas_carga.append(linha['data_execucao'])
                valores_carga.append(float(match_peso.group(1)))
                
    if not datas_carga: datas_carga, valores_carga = ["Sem registros"], [0]
    treino_salvo = db.cursor().execute("SELECT * FROM treinos_gerados WHERE usuario_email = ? AND status = 'ativo' ORDER BY id DESC", (email_aluno,)).fetchone()
    return render_template('evolucao_aluno.html', aluno=aluno, historico=list(historico_treinos)[::-1], datas_linha=json.dumps(datas_carga), cargas_linha=json.dumps(valores_carga), treino_atual=treino_filtro, treinos=extrair_blocos_treino(treino_salvo['treino_texto']) if treino_salvo else {'A':''})

# --- ROTAS DA BIOIMPEDÂNCIA / AVALIAÇÃO FÍSICA ---

@app.route('/progresso', methods=['GET', 'POST'])
def progresso():
    if 'usuario_logado' not in session: return redirect(url_for('login'))
    db = get_db()
    email_alvo = session['usuario_logado']
    aluno_info = db.cursor().execute("SELECT nome, sobrenome, email FROM usuarios WHERE email = ?", (email_alvo,)).fetchone()
    historico_dados = db.cursor().execute("SELECT * FROM historico_progresso WHERE usuario_email = ? ORDER BY id DESC", (email_alvo,)).fetchall()
    return render_template('progresso.html', historico=historico_dados, aluno=aluno_info, modo_visualizacao=False)

@app.route('/progresso/<string:email_aluno>', methods=['GET', 'POST'])
def progresso_aluno(email_aluno):
    if 'usuario_logado' not in session or session['usuario_tipo'] not in ['personal', 'admin']: return redirect(url_for('login'))
    db = get_db()
    aluno_info = db.cursor().execute("SELECT nome, sobrenome, email FROM usuarios WHERE email = ?", (email_aluno,)).fetchone()

    if request.method == 'POST':
        peso = float(request.form.get('peso'))
        altura = float(request.form.get('altura'))
        imc = round(peso / (altura ** 2), 2)
        status_imc = determinar_status_imc(imc)
        
        idade_informada = int(request.form.get('idade_avaliacao') or 25)
        d_torax = float(request.form.get('dobra_torax') or 0)
        d_abdomen = float(request.form.get('dobra_abdomen') or 0)
        d_coxa = float(request.form.get('dobra_coxa') or 0)
        d_triceps = float(request.form.get('dobra_triceps') or 0)
        d_suprailiaca = float(request.form.get('dobra_suprailiaca') or 0)
        
        soma_dobras = 0
        pct_gordura = 0
        
        if d_torax > 0 and d_abdomen > 0:
            soma_dobras = d_torax + d_abdomen + d_coxa
            densidade = 1.10938 - (0.0008267 * soma_dobras) + (0.0000016 * (soma_dobras ** 2)) - (0.0002574 * idade_informada)
            pct_gordura = round(((4.95 / densidade) - 4.50) * 100, 1)
        else:
            soma_dobras = d_triceps + d_suprailiaca + d_coxa
            densidade = 1.099492 - (0.0009929 * soma_dobras) + (0.0000023 * (soma_dobras ** 2)) - (0.0001392 * idade_informada)
            pct_gordura = round(((4.95 / densidade) - 4.50) * 100, 1)
            
        if pct_gordura < 2: pct_gordura = 12.5
        
        massa_gorda = round((peso * pct_gordura) / 100, 2)
        massa_magra = round(peso - massa_gorda, 2)

        db.cursor().execute("""
            INSERT INTO historico_progresso (
                usuario_email, peso, altura, imc, status_imc, braco_esq, braco_dir, cintura, torax, quadril,
                dobra_torax, dobra_abdomen, dobra_coxa, dobra_triceps, dobra_suprailiaca, percentual_gordura, massa_magra, data_registro
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (email_aluno, peso, altura, imc, status_imc, request.form.get('braco_esq'), request.form.get('braco_dir'), 
              request.form.get('cintura'), request.form.get('torax'), request.form.get('quadril'),
              d_torax, d_abdomen, d_coxa, d_triceps, d_suprailiaca, pct_gordura, massa_magra, datetime.now().strftime("%d/%m/%Y")))
        db.commit()
        return redirect(url_for('progresso_aluno', email_aluno=email_aluno))
        
    historico_dados = db.cursor().execute("SELECT * FROM historico_progresso WHERE usuario_email = ? ORDER BY id DESC", (email_aluno,)).fetchall()
    return render_template('progresso.html', historico=historico_dados, aluno=aluno_info, modo_visualizacao=True)

# --- OUTRAS ROTAS ---

@app.route('/personal/agenda', methods=['GET', 'POST'])
def personal_agenda():
    if 'usuario_logado' not in session or session['usuario_tipo'] != 'personal': return redirect(url_for('login'))
    db = get_db()
    email_personal = session['usuario_logado']
    if request.method == 'POST':
        data = request.form.get('data')
        horario = request.form.get('horario')
        vagas = request.form.get('vagas')
        db.cursor().execute("INSERT INTO agenda_disponivel (personal_email, data, horario, vagas_totais) VALUES (?, ?, ?, ?)", (email_personal, data, horario, vagas))
        db.commit()
    horarios = db.cursor().execute("SELECT a.*, COUNT(c.id) as vagas_ocupadas FROM agenda_disponivel a LEFT JOIN checkins_aulas c ON a.id = c.agenda_id WHERE a.personal_email = ? AND a.data >= date('now') GROUP BY a.id ORDER BY a.data ASC, a.horario ASC", (email_personal,)).fetchall()
    return render_template('personal_agenda.html', agenda=[{'info': h, 'alunos': db.cursor().execute("SELECT c.id as checkin_id, c.status, u.nome, u.sobrenome, u.email FROM checkins_aulas c JOIN usuarios u ON c.aluno_email = u.email WHERE c.agenda_id = ?", (h['id'],)).fetchall()} for h in horarios])

@app.route('/personal/agenda/chamada/<int:id_checkin>/<string:novo_status>')
def personal_marcar_chamada(id_checkin, novo_status):
    if 'usuario_logado' not in session or session['usuario_tipo'] != 'personal': return redirect(url_for('login'))
    db = get_db()
    db.cursor().execute("UPDATE checkins_aulas SET status = ? WHERE id = ?", (novo_status, id_checkin))
    db.commit()
    return redirect(url_for('personal_agenda'))

@app.route('/personal/agenda/deletar/<int:id_agenda>')
def personal_deletar_agenda(id_agenda):
    if 'usuario_logado' not in session or session['usuario_tipo'] != 'personal': return redirect(url_for('login'))
    db = get_db()
    db.cursor().execute("DELETE FROM checkins_aulas WHERE agenda_id = ?", (id_agenda,))
    db.cursor().execute("DELETE FROM agenda_disponivel WHERE id = ? AND personal_email = ?", (id_agenda, session['usuario_logado']))
    db.commit()
    return redirect(url_for('personal_agenda'))

@app.route('/aluno/agenda', methods=['GET', 'POST'])
def aluno_agenda():
    if 'usuario_logado' not in session: return redirect(url_for('login'))
    db = get_db()
    email_aluno = session['usuario_logado']
    aluno = db.cursor().execute("SELECT * FROM usuarios WHERE email = ?", (email_aluno,)).fetchone()
    if not aluno['personal_vinculado']: return "Vincule um Personal primeiro.", 400
    if request.method == 'POST' and request.form.get('acao') == 'agendar':
        db.cursor().execute("INSERT INTO checkins_aulas (agenda_id, aluno_email) VALUES (?, ?)", (request.form.get('id_agenda'), email_aluno))
        db.commit()
    return render_template('aluno_agenda.html', disponiveis=db.cursor().execute("SELECT a.*, (a.vagas_totais - COUNT(c.id)) as vagas_restantes FROM agenda_disponivel a LEFT JOIN checkins_aulas c ON a.id = c.agenda_id WHERE a.personal_email = ? AND a.data >= date('now') GROUP BY a.id HAVING vagas_restantes > 0 ORDER BY a.data ASC, a.horario ASC", (aluno['personal_vinculado'],)).fetchall(), meus_agendamentos=[{'checkin_id': ag['checkin_id'], 'data': datetime.strptime(ag['data'], "%Y-%m-%d").strftime("%d/%m/%Y"), 'horario': ag['horario'], 'prof_nome': ag['prof_nome'], 'status': ag['status'], 'pode_desmarcar': True} for ag in db.cursor().execute("SELECT c.id as checkin_id, c.status, a.data, a.horario, u.nome as prof_nome FROM checkins_aulas c JOIN agenda_disponivel a ON c.agenda_id = a.id JOIN usuarios u ON a.personal_email = u.email WHERE c.aluno_email = ? ORDER BY a.data ASC, a.horario ASC", (email_aluno,)).fetchall()])

@app.route('/personal/revisar/<int:id_treino>', methods=['GET', 'POST'])
def personal_revisar_treino(id_treino):
    if 'usuario_logado' not in session or session['usuario_tipo'] != 'personal': return redirect(url_for('login'))
    db = get_db()
    treino = db.cursor().execute("SELECT t.*, u.nome, u.sobrenome FROM treinos_gerados t JOIN usuarios u ON t.usuario_email = u.email WHERE t.id = ?", (id_treino,)).fetchone()
    if request.method == 'POST':
        blocos_final = [f"MARCADOR_TREINO_{letra}\n{request.form.get(f'treino_{letra}', '').strip()}" for letra in ['A', 'B', 'C', 'D', 'E', 'F'] if request.form.get(f'treino_{letra}', '').strip()]
        db.cursor().execute("UPDATE treinos_gerados SET status = 'arquivado' WHERE usuario_email = ? AND status = 'ativo'", (treino['usuario_email'],))
        db.cursor().execute("UPDATE treinos_gerados SET treino_texto = ?, status = 'ativo' WHERE id = ?", ("\n\n".join(blocos_final), id_treino))
        db.commit()
        return redirect(url_for('painel_personal'))
    return render_template('revisar_treino.html', treino=treino, blocos=extrair_blocos_treino(treino['treino_texto']))

@app.route('/personal/recusar/<int:id_treino>')
def personal_recusar_treino(id_treino):
    if 'usuario_logado' not in session or session['usuario_tipo'] != 'personal': return redirect(url_for('login'))
    db = get_db()
    db.cursor().execute("UPDATE treinos_gerados SET status = 'rejeitado' WHERE id = ?", (id_treino,))
    db.commit()
    return redirect(url_for('painel_personal'))

@app.route('/', methods=['GET', 'POST'])
def index():
    if 'usuario_logado' not in session: return redirect(url_for('login'))
    db = get_db()
    email_aluno = session['usuario_logado']
    aluno_status = db.cursor().execute("SELECT personal_vinculado FROM usuarios WHERE email = ?", (email_aluno,)).fetchone()
    treino_analise = db.cursor().execute("SELECT * FROM treinos_gerados WHERE usuario_email = ? AND status = 'pendente'", (email_aluno,)).fetchone()
    if treino_analise: return redirect(url_for('meus_treinos'))
    
    erro_vinculo = None
    if request.method == 'POST':
        novo_personal_email = request.form.get('novo_personal_email', '').strip().lower()
        limita = request.form.get('limitacoes', '').strip()
        
        if not aluno_status['personal_vinculado']:
            if not novo_personal_email:
                erro_vinculo = "Você deve informar o e-mail do seu professor!"
                return render_template('index.html', aluno_sem_professor=True, erro_vinculo=erro_vinculo)
            validar_prof = db.cursor().execute("SELECT email FROM usuarios WHERE email = ? AND tipo = 'personal'", (novo_personal_email,)).fetchone()
            if not validar_prof:
                erro_vinculo = "Este e-mail de professor não foi localizado no sistema!"
                return render_template('index.html', aluno_sem_professor=True, erro_vinculo=erro_vinculo)
            db.cursor().execute("UPDATE usuarios SET personal_vinculado = ?, limitacoes = ? WHERE email = ?", (novo_personal_email, limita, email_aluno))
        else:
            db.cursor().execute("UPDATE usuarios SET limitacoes = ? WHERE email = ?", (limita, email_aluno))
            
        db.commit()
        dias = request.form.get('idade', '5').strip()
        obj, fc = request.form.get('objetivo'), request.form.get('foco')
        
        try:
            treino_gerado = chamar_ia_groq(obj, fc, 'Intermediário', dias, limita)
            db.cursor().execute('INSERT INTO treinos_gerados (usuario_email, treino_texto, data_criacao, data_validade, status, nivel, foco, objetivo) VALUES (?, ?, ?, ?, "pendente", "Intermediário", ?, ?)', (email_aluno, treino_gerado, datetime.now().strftime("%d/%m/%Y"), (datetime.now() + timedelta(days=60)).strftime("%d/%m/%Y"), fc, obj))
            db.commit()
            return redirect(url_for('meus_treinos'))
        except Exception as e: return f"Erro: {e}"
        
    sem_professor = True if not aluno_status['personal_vinculado'] else False
    return render_template('index.html', aluno_sem_professor=sem_professor, erro_vinculo=erro_vinculo)

@app.route('/meus_treinos')
def meus_treinos():
    if 'usuario_logado' not in session: return redirect(url_for('login'))
    db = get_db()
    return render_template('meus_treinos.html', ativo=db.cursor().execute("SELECT * FROM treinos_gerados WHERE usuario_email = ? AND status = 'ativo' ORDER BY id DESC", (session['usuario_logado'],)).fetchone(), pendente=db.cursor().execute("SELECT * FROM treinos_gerados WHERE usuario_email = ? AND status = 'pendente' ORDER BY id DESC", (session['usuario_logado'],)).fetchone(), antigos=db.cursor().execute("SELECT * FROM treinos_gerados WHERE usuario_email = ? AND status = 'arquivado' ORDER BY id DESC", (session['usuario_logado'],)).fetchall())

@app.route('/iniciar_treino', methods=['GET', 'POST'])
def iniciar_treino():
    if 'usuario_logado' not in session: return redirect(url_for('login'))
    db = get_db()
    if request.method == 'POST':
        db.cursor().execute('INSERT INTO historico_treino (usuario_email, tipo_treino, cargas_anotadas, data_execucao) VALUES (?, ?, ?, ?)', (session['session_logado'], request.form.get('tipo_treino'), request.form.get('cargas'), datetime.now().strftime("%d/%m/%Y")))
        db.commit()
        return redirect(url_for('historico'))
    treino_salvo = db.cursor().execute("SELECT * FROM treinos_gerados WHERE usuario_email = ? AND status = 'ativo' ORDER BY id DESC", (session['usuario_logado'],)).fetchone()
    return render_template('iniciar_treino.html', treinos=extrair_blocos_treino(treino_salvo['treino_texto']) if treino_salvo else {'A':''})

@app.route('/historico')
def historico():
    if 'usuario_logado' not in session: return redirect(url_for('login'))
    db = get_db()
    treino_filtro = request.args.get('treino_filtro', 'TREINO A').upper().strip()
    historico_dados = db.cursor().execute("SELECT * FROM historico_treino WHERE usuario_email = ? ORDER BY id ASC", (session['usuario_logado'],)).fetchall()
    datas_linha, cargas_linha = [], []
    for line in historico_dados:
        if line['tipo_treino'].upper().strip() == treino_filtro:
            match_peso = re.search(r'Carga:\s*(\d+)', line['cargas_anotadas'], re.IGNORECASE)
            if not match_peso: match_peso = re.search(r'(\d+)', line['cargas_anotadas'])
            if match_peso: datas_linha.append(line['data_execucao']); cargas_linha.append(int(match_peso.group(1)))
    if not datas_linha: datas_linha, cargas_linha = ["Sem dados"], [0]
    return render_template('historico.html', historico=list(historico_dados)[::-1], datas_linha=json.dumps(datas_linha), cargas_linha=json.dumps(cargas_linha), treino_atual=treino_filtro)

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)
