class ServiceDomainMiddleware:
    """Give service.pinel.ru its own public root without changing checklist URLs."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.get_host().partition(':')[0].lower() == 'service.pinel.ru':
            request.urlconf = 'warranty.service_urls'
        return self.get_response(request)
