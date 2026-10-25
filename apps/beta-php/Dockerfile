# Cliente 2 - Beta POS (PHP 8.3 + Apache)
FROM php:8.3-apache

# Extensión para conectarse a MySQL con PDO
RUN docker-php-ext-install pdo_mysql

# Configuración de PHP para producción.
# expose_php=On deja la cabecera "X-Powered-By: PHP/8.3", que Wappalyzer usa para detectar el lenguaje.
RUN mv "$PHP_INI_DIR/php.ini-production" "$PHP_INI_DIR/php.ini" \
 && printf "expose_php=On\nsession.cookie_httponly=1\nsession.use_strict_mode=1\n" > "$PHP_INI_DIR/conf.d/beta.ini"

# Apache escucha en 8080 para no requerir el puerto privilegiado 80 dentro del contenedor
RUN sed -i 's/Listen 80/Listen 8080/' /etc/apache2/ports.conf \
 && sed -i 's/:80>/:8080>/' /etc/apache2/sites-available/000-default.conf

COPY src/ /var/www/html/
RUN chown -R www-data:www-data /var/www/html

EXPOSE 8080
