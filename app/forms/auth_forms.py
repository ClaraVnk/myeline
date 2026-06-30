from flask_wtf import FlaskForm
from wtforms import PasswordField, StringField, SubmitField
from wtforms.validators import DataRequired, Email, EqualTo, ValidationError


class ForgotPasswordForm(FlaskForm):
    email = StringField("Adresse email", validators=[DataRequired(), Email()])
    submit = SubmitField("Réinitialiser mon mot de passe")


def _strong_password(form, field):
    from app.services.user_services import is_strong_password
    if not is_strong_password(field.data):
        raise ValidationError(
            "Le mot de passe doit contenir au moins 8 caractères, "
            "une majuscule, une minuscule, un chiffre et un caractère spécial."
        )


class ResetPasswordForm(FlaskForm):
    password = PasswordField(
        "Nouveau mot de passe", validators=[DataRequired(), _strong_password]
    )
    confirm_password = PasswordField(
        "Confirmer le mot de passe",
        validators=[
            DataRequired(),
            EqualTo("password", message="Les mots de passe doivent correspondre."),
        ],
    )
    submit = SubmitField("Réinitialiser")
