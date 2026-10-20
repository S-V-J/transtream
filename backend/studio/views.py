from django.contrib.auth.decorators import login_required
from django.shortcuts import redirect, render


def root_redirect(request):
    """
    / → if authenticated → /studio/
       if not            → /accounts/login/
    """
    if request.user.is_authenticated:
        return redirect("studio:studio")
    return redirect("login")


@login_required
def studio(request):
    return render(request, "studio.html", {"page": "studio"})


@login_required
def settings_page(request):
    return render(request, "settings.html", {"page": "settings"})


@login_required
def account(request):
    return render(request, "account.html", {"page": "account"})


@login_required
def about(request):
    return render(request, "about.html", {"page": "about"})
