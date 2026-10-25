#!/bin/bash
# Prueba de velocidad y de carga · Cliente 1 (alpha) vs Cliente 2 (beta)
SEG=${1:-120}; PAR=${2:-3}
OUT=~/paas-parcial2/pruebas/resultados-carga.txt
HOSTS="alpha.paas.local beta.paas.local"
echo "== Velocidad en reposo (20 peticiones HTTPS por cliente) $(date '+%F %T')" | tee $OUT
printf "%-18s %8s %9s %8s\n" Cliente "TLS ms" "Total ms" "Max ms" | tee -a $OUT
for h in $HOSTS; do
  for i in $(seq 20); do curl -sk -o /dev/null -w "%{time_appconnect} %{time_total}\n" https://$h/; done |
  awk -v h=$h '{t+=$1; x+=$2; if($2>m) m=$2} END {printf "%-18s %8.1f %9.1f %8.1f\n", h, t/NR*1000, x/NR*1000, m*1000}' | tee -a $OUT
done
echo; echo "== Carga: $PAR procesos por cliente durante $SEG s (los dos a la vez)" | tee -a $OUT
for h in $HOSTS; do
  ( end=$((SECONDS+SEG)); for i in $(seq $PAR); do
      ( while [ $SECONDS -lt $end ]; do curl -sk -o /dev/null -w "%{http_code} %{time_total}\n" https://$h/; done ) &
    done; wait ) > /tmp/carga-$h.txt &
done
for s in $(seq $SEG -10 1); do printf "\r  quedan %3d s... " $s; sleep 10; done; wait; echo
printf "%-18s %10s %9s %9s %11s %9s\n" Cliente Peticiones "HTTP 200" "HTTP 503" "Acept/min" "Prom ms" | tee -a $OUT
for h in $HOSTS; do
  awk -v h=$h -v s=$SEG '{n++; c[$1]++; t+=$2} END {printf "%-18s %10d %9d %9d %11.0f %9.1f\n", h, n, c["200"], c["503"], c["200"]/s*60, t/n*1000}' /tmp/carga-$h.txt | tee -a $OUT
done
echo "Resultados guardados en $OUT"
