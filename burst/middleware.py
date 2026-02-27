import re
import os

class RangeFileMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)

        if request.path.startswith('/media/') and hasattr(response, 'file_to_stream'):
            return self.handle_range(request, response)

        return response

    def handle_range(self, request, response):
        range_header = request.META.get('HTTP_RANGE', '')
        if not range_header:
            response['Accept-Ranges'] = 'bytes'
            return response

        file = response.file_to_stream
        file.seek(0, 2)
        file_size = file.tell()

        match = re.match(r'bytes=(\d+)-(\d*)', range_header)
        if not match:
            response['Accept-Ranges'] = 'bytes'
            return response

        start = int(match.group(1))
        end = int(match.group(2)) if match.group(2) else file_size - 1
        end = min(end, file_size - 1)
        length = end - start + 1

        file.seek(start)
        data = file.read(length)

        from django.http import HttpResponse
        range_response = HttpResponse(data, status=206, content_type=response.get('Content-Type', 'video/mp4'))
        range_response['Content-Range'] = f'bytes {start}-{end}/{file_size}'
        range_response['Accept-Ranges'] = 'bytes'
        range_response['Content-Length'] = str(length)
        return range_response