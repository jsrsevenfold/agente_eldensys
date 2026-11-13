# EldenSys Agent — regras do repositório para a IA

## Arquivos que nunca devem ser lidos inteiros

- `C:\DEV\EldenSys\frontend\src\v2\pages\produtos\etiquetas\useEtiquetas.ts`
  (mais de 1.600 linhas, ~72 KB): nunca leia este arquivo inteiro. Procure
  nele com Grep, ou leia só as linhas necessárias (Read com `offset` e
  `limit`).
