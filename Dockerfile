FROM python:3.13-alpine

RUN adduser -D user
RUN mkdir -p /opt/rdgen/data /opt/rdgen/exe /opt/rdgen/png /opt/rdgen/temp_zips \
 && chown -R user:user /opt/rdgen

WORKDIR /opt/rdgen

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY --chown=user:user . .
USER user

ENV PYTHONUNBUFFERED=1

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --retries=3 CMD python -m rdgen.healthcheck

CMD ["sh", "-c", "python manage.py migrate --noinput && exec gunicorn -c gunicorn.conf.py rdgen.wsgi:application"]
