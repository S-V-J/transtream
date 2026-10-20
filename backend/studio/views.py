from django.shortcuts import render


def studio(request):
    return render(request, "studio.html", {"page": "studio"})


def settings_page(request):
    return render(request, "settings.html", {"page": "settings"})


def account(request):
    return render(request, "account.html", {"page": "account"})


def about(request):
    return render(request, "about.html", {"page": "about"})
