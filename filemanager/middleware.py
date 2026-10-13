from django.shortcuts import redirect
from django.urls import reverse

from .models import UserProfile


class PasswordChangeRequiredMiddleware:
    """Redirect users who have not changed their initial password to the change form."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.user.is_authenticated and not self.has_changed_password(
            request.user
        ):
            allowed_paths = {reverse('change_password'), reverse('logout')}
            if request.path not in allowed_paths:
                return redirect('change_password')
        return self.get_response(request)

    @staticmethod
    def has_changed_password(user):
        return UserProfile.objects.filter(user=user, password_changed=True).exists()
