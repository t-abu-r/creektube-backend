from django.contrib.auth.tokens import PasswordResetTokenGenerator

# Token generator for email verification
account_activation_token = PasswordResetTokenGenerator()