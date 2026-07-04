from app import app, db, Usuario, bcrypt

with app.app_context():
    # Verifica se o usuário já existe
    usuario_existente = Usuario.query.filter_by(email='admin@igreja.com').first()

    if usuario_existente:
        print("⚠️ Usuário admin já existe!")
    else:
        # Gera hash usando o mesmo bcrypt do app
        senha_hash = bcrypt.generate_password_hash('admin123').decode('utf-8')

        novo_usuario = Usuario(
            nome='Administrador',
            email='admin@igreja.com',
            senha=senha_hash,
            cargo='admin'
        )

        db.session.add(novo_usuario)
        db.session.commit()
        print("✅ Usuário administrador criado com sucesso!")
