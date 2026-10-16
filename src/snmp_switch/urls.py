# pib/snmp_switch/urls.py

from django.urls import path

from snmp_switch.views import link, power, status, toggle

urlpatterns = [
    path('status', status),
    path('toggle', toggle),
    path('power', power),
    path('link', link),
]
