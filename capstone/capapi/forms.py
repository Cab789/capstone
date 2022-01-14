import re
import requests

from django.conf import settings
from django.contrib.auth.forms import AuthenticationForm, UserCreationForm
from django import forms
from django.utils.safestring import mark_safe
from django.utils.text import format_lazy

from capapi.models import CapUser, ResearchContract, HarvardContract, EmailBlocklist
from capweb.helpers import reverse, reverse_lazy
from config.logging import logger


class LoginForm(AuthenticationForm):
    username = forms.EmailField(
        max_length=254,
        widget=forms.EmailInput(attrs={'autofocus': True}),
    )

    def confirm_login_allowed(self, user):
        """ Override AuthenticationForm to block login with unverified email address. """
        if not user.email_verified:
            raise forms.ValidationError(
                mark_safe("This email is registered but not yet verified. <a href='%s'>Resend verification</a>?" % reverse('resend-verification')),
                code='unverified',
            )
        return super().confirm_login_allowed(user)


class ResendVerificationForm(forms.Form):
    email = forms.EmailField(label='Email', max_length=255)


class RegisterUserForm(UserCreationForm):
    agreed_to_tos = forms.BooleanField()
    mailing_list = forms.BooleanField(initial=True, required=False)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # set label here because reverse() isn't ready when defining the class
        self.fields['mailing_list'].label = mark_safe("<small>(optional)</small> Sign me up for the CAP newsletter: Lawvocado.")
        self.fields['agreed_to_tos'].label = mark_safe("I have read and agree to the <a href='%s' target='_blank'>Terms of Use</a>." % reverse('terms'))

    class Meta:
        model = CapUser
        fields = ["email", "first_name", "last_name", "password1", "password2", "agreed_to_tos", "mailing_list"]

    def clean_email(self):
        """ Ensure that email address doesn't match an existing CapUser.normalized_email. """
        email = self.cleaned_data.get("email")
        if re.search(r'\s', email):
            raise forms.ValidationError("Email address may not contain spaces.")
        if CapUser.objects.filter(normalized_email=CapUser.normalize_email(email)).exists():
            raise forms.ValidationError("A user with the same email address has already registered.")

        # validate email against blocklists
        if not EmailBlocklist.email_allowed(email):
            logger.warning("Email address blocked: %s" % email)
            raise forms.ValidationError("This email address is invalid. If you believe this is an error, please contact us.")

        # validate email against mailgun api
        if settings.VALIDATE_EMAIL_SIGNUPS:
            try:
                response = requests.get(
                    "https://api.mailgun.net/v4/address/validate",
                    auth=("api", settings.MAILGUN_API_KEY),
                    params={"address": email})
                response.raise_for_status()
            except requests.RequestException:
                raise forms.ValidationError("Cannot connect to email validation server. If this problem persists, please contact us.")
            response_json = response.json()
            if response_json['result'] == 'undeliverable' or (response_json['result'] == 'do_not_send' and 'mailbox_is_disposable_address' in response_json['reason']):
                # reject undeliverable addresses and disposable addresses
                logger.warning("Invalid email address: %s" % response_json)
                raise forms.ValidationError("This email address is invalid. If you believe this is an error, please contact us.")

        return email

    def save(self, commit=True):
        user = super().save(commit)
        user.create_nonce()
        return user


class ResearchContractForm(forms.ModelForm):
    name = forms.CharField(label='Full name of researcher')
    email = forms.EmailField(
        disabled=True,  # any email submitted by user will be ignored
        help_text=format_lazy(
            "For faster approval, make sure you are applying from a CAP account with an email address "
            "provided by your institution. If this is the wrong email address, <a href='{}'>create an account</a> "
            "with the correct address before applying.", reverse_lazy('register')))
    institution = forms.CharField(label='Academic or non-profit research institution (if any)', required=False)
    title = forms.CharField(label='Title or Affiliation', required=False)
    area_of_interest = forms.CharField(label='Research area of interest (optional and non-binding)', widget=forms.Textarea, required=False)

    class Meta:
        model = ResearchContract
        fields = ["name", "email", "institution", "title", "area_of_interest"]


class HarvardContractForm(forms.ModelForm):
    name = forms.CharField(label='Your full name')
    title = forms.CharField(label='Your current title or affiliation at Harvard')
    area_of_interest = forms.CharField(label='Research area of interest (optional and non-binding)', widget=forms.Textarea, required=False)

    class Meta:
        model = HarvardContract
        fields = ["name", "title", "area_of_interest"]
