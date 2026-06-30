from flask_wtf import FlaskForm
from wtforms import BooleanField, PasswordField, StringField, SubmitField, TextAreaField
from wtforms.validators import DataRequired, Email, EqualTo, Length, ValidationError

from app.models import User


class LoginForm(FlaskForm):
    email = StringField(
        "Email",
        validators=[
            DataRequired(message="Ce champ est requis."),
            Email(message="Adresse email invalide."),
        ],
        render_kw={"placeholder": "Votre adresse email"},
    )
    password = PasswordField(
        "Mot de passe",
        validators=[
            DataRequired(message="Ce champ est requis."),
            Length(
                min=8, message="Le mot de passe doit contenir au moins 8 caractères."
            ),
        ],
    )
    submit = SubmitField("Se connecter")


class RegisterForm(FlaskForm):
    firstname = StringField(
        "Prénom",
        validators=[
            DataRequired(message="Ce champ est requis."),
            Length(min=2, max=100),
        ],
        render_kw={"placeholder": "Votre prénom"},
    )
    email = StringField(
        "Email",
        validators=[
            DataRequired(message="Ce champ est requis."),
            Email(message="Adresse email invalide."),
        ],
        render_kw={"placeholder": "Votre adresse email"},
    )
    lastname = StringField(
        "Nom",
        validators=[
            DataRequired(message="Ce champ est requis."),
            Length(min=2, max=100),
        ],
        render_kw={"placeholder": "Votre nom"},
    )
    password = PasswordField(
        "Mot de passe",
        validators=[
            DataRequired(message="Ce champ est requis."),
            Length(min=6, message="Minimum 6 caractères."),
        ],
    )
    confirm_password = PasswordField(
        "Confirmer le mot de passe",
        validators=[
            DataRequired(message="Veuillez confirmer votre mot de passe."),
            EqualTo("password", message="Les mots de passe doivent correspondre."),
        ],
    )
    accept_cgv = BooleanField(
        "J'accepte les CGV",
        validators=[DataRequired(message="Vous devez accepter les CGV.")],
    )
    accept_privacy = BooleanField(
        "J'accepte la politique de confidentialité",
        validators=[DataRequired(message="Vous devez accepter la politique de confidentialité.")],
    )
    submit = SubmitField("S'inscrire")

    def validate_email(self, field):
        """Validate that email is not already registered.

        Args:
            field: Form field being validated

        Raises:
            ValidationError: If email is already in use
        """
        if User.query.filter_by(email=field.data).first():
            raise ValidationError("Cet email est déjà utilisé.")


class ContactForm(FlaskForm):
    email = StringField(
        "Email",
        validators=[
            DataRequired(message="Ce champ est requis."),
            Email(message="Adresse email invalide."),
        ],
        render_kw={"placeholder": "Votre adresse email"},
    )
    message = TextAreaField(
        "Message",
        validators=[
            DataRequired(message="Ce champ est requis."),
            Length(min=10, message="Minimum 10 caractères."),
        ],
        render_kw={"placeholder": "Votre message"},
    )
    submit = SubmitField("Envoyer")
