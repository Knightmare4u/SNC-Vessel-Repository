from django import forms
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import models

from .models import SigningRequest
from .services.signing import (
    saved_signature_png,
    signature_from_data_url,
    signature_from_upload,
)
from .utils import display_name

User = get_user_model()

MAX_SIGNERS = 20


def signable_users():
    # Users without an email can take part; they just get no notifications
    return User.objects.filter(is_active=True).order_by('username')


class UserLabelMixin:
    def label_from_instance(self, user):
        name = display_name(user)
        return f'{name} <{user.email}>' if user.email else name


class UserChoiceField(UserLabelMixin, forms.ModelChoiceField):
    pass


class UserMultipleChoiceField(UserLabelMixin, forms.ModelMultipleChoiceField):
    pass


class SigningCreateForm(forms.Form):
    source_path = forms.CharField(widget=forms.HiddenInput)
    placement_mode = forms.ChoiceField(
        choices=[
            (SigningRequest.PlacementMode.AUTO, 'Add a signature page at the end'),
            (
                SigningRequest.PlacementMode.MANUAL,
                'Place each signature on the document',
            ),
        ],
        initial=SigningRequest.PlacementMode.AUTO,
        widget=forms.RadioSelect,
    )
    # Plain multi-select; the page turns it into a searchable dropdown (select2)
    notify_users = UserMultipleChoiceField(queryset=signable_users(), required=False)

    def __init__(self, *args, user, **kwargs):
        super().__init__(*args, **kwargs)
        # The sender is emailed on completion anyway, so not offered here
        self.fields['notify_users'].queryset = signable_users().exclude(pk=user.pk)


class SignerForm(forms.Form):
    user = UserChoiceField(queryset=signable_users())


class BaseSignerFormSet(forms.BaseFormSet):
    # The page drags rows to reorder and hides removed ones; both are set by JS
    ordering_widget = forms.HiddenInput
    deletion_widget = forms.HiddenInput

    def clean(self):
        if any(self.errors):
            return
        # Removed rows are skipped, as in Django's custom formset validation docs
        forms_kept = [
            form
            for form in self.forms
            if not (self.can_delete and self._should_delete_form(form))
        ]
        # Rows past min_num may be left blank and still pass as valid forms
        if any('user' not in form.cleaned_data for form in forms_kept):
            raise ValidationError('Choose a person for every signer row.')
        users = [form.cleaned_data['user'] for form in forms_kept]
        if len(set(users)) != len(users):
            raise ValidationError('Each signer can only be added once.')


# Signing order comes from each form's ORDER field (formset.ordered_forms)
SignerFormSet = forms.formset_factory(
    SignerForm,
    formset=BaseSignerFormSet,
    extra=0,
    min_num=1,
    validate_min=True,
    max_num=MAX_SIGNERS,
    validate_max=True,
    can_order=True,
    can_delete=True,
)


class SigningFieldsForm(forms.Form):
    # Filled by the placement page's script; signing_service.save_fields() checks it
    # Not "fields": that would shadow Form.fields
    boxes = forms.JSONField(widget=forms.HiddenInput)

    def clean_boxes(self):
        boxes = self.cleaned_data['boxes']
        if not isinstance(boxes, list) or not all(isinstance(b, dict) for b in boxes):
            raise ValidationError('Invalid signature boxes.')
        return boxes


class SignForm(forms.Form):
    """One of three signature sources; clean() turns it into
    cleaned_data['signature_png']."""

    class Source(models.TextChoices):
        SAVED = 'saved', 'My signature'
        DRAWN = 'drawn', 'Draw'
        UPLOADED = 'uploaded', 'Upload'

    source = forms.ChoiceField(choices=Source, widget=forms.HiddenInput)
    signature = forms.CharField(required=False, widget=forms.HiddenInput)
    signature_file = forms.FileField(
        required=False,
        widget=forms.FileInput(attrs={'accept': 'image/png,image/jpeg'}),
    )
    save_as_default = forms.BooleanField(required=False)

    def __init__(self, *args, user, **kwargs):
        super().__init__(*args, **kwargs)
        self.user = user

    def clean(self):
        cleaned_data = super().clean()
        source = cleaned_data.get('source')
        if source == self.Source.SAVED:
            png = saved_signature_png(self.user)
            if png is None:
                raise ValidationError('You have no saved signature yet.')
            # It already is the default; saving it again would only copy the file
            cleaned_data['save_as_default'] = False
        elif source == self.Source.DRAWN:
            if not cleaned_data.get('signature'):
                raise ValidationError('Please draw your signature first.')
            png = signature_from_data_url(cleaned_data['signature'])
        elif source == self.Source.UPLOADED:
            if not cleaned_data.get('signature_file'):
                raise ValidationError('Please choose an image to upload.')
            png = signature_from_upload(cleaned_data['signature_file'])
        else:
            return cleaned_data  # The source field already has an error
        cleaned_data['signature_png'] = png
        return cleaned_data
