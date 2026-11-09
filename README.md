# SolSnipBot — Solana Sniper Trading Bot

Bot de snipe **Solana** en temps réel. Il écoute en continu vos canaux Telegram,
extrait les adresses de contrats (CA), analyse chaque token avec les données
on-chain **GMGN**, applique vos critères de snipe, achète automatiquement les
tokens valides, puis gère la position en direct (Take Profit / Stop Loss /
Trailing Stop) sur les prix **Dexscreener**.

> ⚠️ **Données 100 % réelles.** Aucune donnée simulée ou fictive n'est utilisée
> dans l'application : prix, market cap, holders, KOLs, trades… tout provient des
> API GMGN et Dexscreener en direct.

---

## Fonctionnalités

- **Écoute Telegram continue** des canaux `pumpdotfunalert`, `gmgnsignals`,
  `ai_agent_solana_0xbot` (API officielle, session persistante).
- **Extraction automatique** des adresses Solana (base58) dans les messages.
- **Analyse GMGN** de chaque call : market cap, volume, top 10 holders,
  acheteurs/vendeurs, âge, site web, réseaux sociaux, KOLs, smart wallets.
- **Moteur de décision** transparent : chaque critère est affiché avec sa valeur
  et son seuil (voir le panneau *Détails du Token*).
- **Exécution réelle** des achats/ventes, au choix :
  - **Jupiter** (par défaut) — Jupiter construit le swap, la clé du wallet signe
    **localement**, la transaction part vers un nœud RPC. **Aucun plan GMGN requis.**
  - **GMGN** — `gmgn-cli swap` (routage GMGN), qui exige un plan GMGN payant.
- **Mode papier** (interrupteur **Papier / Réel** dans la barre du haut, ou
  `paper_trading`) — le pipeline entier tourne pour de vrai (call Telegram réel,
  analyse GMGN réelle, prix Dexscreener réels, devis **et simulation** d'une route
  Jupiter réelle) mais **aucune transaction n'est signée ni diffusée**. Détails dans
  les sections *Mode papier* et *Basculer papier / réel* ci-dessous.
- **Suivi temps réel** du prix & du PnL, rafraîchi en continu.
  - **Fermeture manuelle** d'une position depuis le tableau de bord (croix ✕ ou
    bouton « Fermer ») : une **confirmation** propose **25 / 50 / 100 %**. Une
    vente partielle laisse la position ouverte, une vente totale fige son PnL.
    Un refus (quote rate-limitée, position déjà fermée) s'affiche dans la boîte
    et laisse la position intacte. Détails dans *Fermer une position à la main*.
  - **Un seul flux Dexscreener** pour toute l'application (`backend/pricefeed.py`).
    Toutes les 2 s (réglable), une **requête groupée** ramène le prix de tout ce
    qui mérite d'être suivi : les **positions ouvertes** (elles pilotent TP/SL),
    le **token sélectionné** dans le tableau de bord, la **liste de suivi**
    ajoutée à la main, et les **calls les plus récents** — le tableau de bord
    reste donc vivant entre deux trades. Chaque tick est poussé par WebSocket :
    le panneau **Suivi de prix en direct** et le graphique bougent tick par tick,
    sans attendre un rafraîchissement à la minute.
  - **Un seul poller, pas deux** : le gestionnaire de positions **lit** les
    derniers ticks du flux au lieu d'interroger Dexscreener de son côté, et ne
    fait une lecture directe que si le tick est trop vieux (flux en pause, en
    erreur ou désactivé) — la protection ne dépend donc jamais d'un flux arrêté.
  - **Anti-rate-limit** : une requête par intervalle (Dexscreener accepte 30
    adresses par requête), plafond à 30 tokens suivis, et **recul exponentiel**
    (jusqu'à 30 s) dès qu'une requête échoue, avec retour au rythme normal et
    message dans le journal.
  - **Aucune donnée simulée** : un token que Dexscreener n'indexe pas encore
    (lancement récent) est affiché **« aucun tick »** avec son nombre de
    tentatives sans prix — jamais un prix inventé ni un zéro. Un flux qui
    s'arrête garde le dernier tick **avec son âge**, et le badge passe à
    `flux en retard` puis `pause N s`.
  - **Graphique non trafiqué** : les chandeliers viennent de **GMGN**, les ticks
    de **Dexscreener** — deux sources indépendantes qui peuvent diverger
    fortement sur un token très récent (écart mesuré : **×5,7**). Un tick trop
    éloigné de la série tracée n'est **pas dessiné** (cela inventerait une mèche
    qu'aucun trade n'a produite) : il est signalé par une pastille
    `graphique ×N`. Dans la bande de cohérence, le chandelier en cours grandit
    en direct.
- **Gestion de position** configurable : Take Profit, Stop Loss, Trailing Stop.
  - Le **prix d'entrée** est le prix de remplissage **réel** (SOL dépensés ÷
    tokens reçus, lus sur la chaîne), pas le prix affiché au moment de l'analyse :
    les seuils TP/SL portent donc sur la vraie position.
  - Le **Trailing Stop** s'arme dès que la position est **en profit**, puis sort
    si le prix retombe du pourcentage configuré sous son **plus haut**.
  - Le **PnL réalisé** d'une clôture est calculé sur les **SOL réellement
    reçus** (delta on-chain, mesuré lamport par lamport) et il **inclut tous les
    frais de transaction** : frais de réseau **et** frais de priorité de l'achat,
    de la vente et de la transaction de fermeture du compte de token.

    ```
    PnL net = (SOL reçus de la vente + frais de vente) − SOL engagés − frais (achat + vente + clôture)
    ```

    Ce ne sont pas des frais théoriques : sur un trade de 0,001 SOL ils pèsent
    couramment **6 à 15 %**, soit plus que l'essentiel du résultat — un gain de
    +18 % avant frais peut se réduire à +7 %. Le **loyer** du compte de token en
    est exclu (c'est un remboursement, voir ci-dessous). Toute position fermée
    conserve le total payé dans `fees_sol`.
  - **Libellé juste** — un résultat net qui contredit le nom de la règle est
    affiché comme tel : un Take Profit soldé en **perte** porte un badge
    `net négatif`, une sortie protectrice soldée en **gain** un badge
    `net positif`, chacun avec l'explication en infobulle. La colonne s'appelle
    **PnL net**, et son infobulle rappelle les frais inclus et leur montant.
  - **Sortie contrôlée par le prix exécutable** : le prix affiché (Dexscreener)
    d'un token tout juste lancé peut s'écarter fortement du marché (pool pas
    encore indexé, `liquidité $0`). Avant d'exécuter une sortie, le bot demande
    un **devis Jupiter réel** sur la quantité détenue, et les deux règles sont
    traitées différemment :
    - un **Take Profit n'est validé que si le prix exécutable est lui aussi au
      niveau du Take Profit** : un bénéfice que personne ne paie n'existe pas, la
      sortie est donc **annulée** et la position continue de courir ;
    - une sortie **protectrice** (Stop Loss / Trailing Stop) n'est jamais
      affaiblie : elle s'applique dès que les deux prix concordent, ou partout où
      le prix exécutable confirme le franchissement ; en cas d'écart important
      elle est recalculée pour qu'un prix affiché erroné ne déclenche pas un stop
      sur un prix qu'aucun acheteur ne propose.

    Un devis indisponible ne désactive jamais la protection : la sortie décidée
    est maintenue. Le journal indique `Sortie « … » recalculée` (au plus une fois
    par minute et par token).

    **Ce que l'écran montre** : la ligne de PnL d'une position reste celle du prix
    Dexscreener — c'est le marché affiché — mais dès qu'une sortie est décidée puis
    recalculée sur le prix exécutable, la ligne porte un badge
    (`TP atteint, non exécutable`, `SL non exécutable`, `Trailing non exécutable`)
    et, à côté du PnL affiché, le **PnL au prix exécutable**. L'infobulle donne le
    prix affiché, le prix exécutable, l'écart en pourcentage et la règle
    réellement appliquée. Sans cela, un token dont le prix affiché atteint +39 %
    alors que le marché n'en paie que −2 % montrait un Take Profit qui « ne s'est
    pas déclenché », sans la moindre explication sur la ligne. Le badge disparaît
    au premier tick où les deux prix sont d'accord : il décrit l'instant, pas un
    historique.

    Écart mesuré au repos : **+1,3 % à +2,2 %** entre l'affichage et le prix
    exécutable ; sur 18 relevés successifs, **aucun** écart au-delà de 10 % — les
    30 à 55 % qui annulent un TP sont des pics isolés, pas un biais permanent.
  - Le **prix de sortie réalisé** est enregistré à la clôture (`exit_price`),
    mesuré comme les SOL reçus rapportés aux tokens vendus — et non le prix
    affiché. C'est un **taux hors frais**, donc directement comparable au prix
    d'entrée (lui aussi un taux hors frais) : l'écart entre les deux prix montre
    le mouvement du marché, l'écart entre ce taux et le PnL montre ce que les
    frais ont coûté.
  - Une vente partielle répartit le coût au prorata, et un pourcentage qui
    arrondirait à **zéro unité est refusé** au lieu de vendre toute la position.
- **Récupération du loyer** — chaque premier achat crée un compte de token dont
  le loyer (~0,0015–0,002 SOL) reste bloqué. À la clôture d'une position, le bot
  **ferme ce compte et récupère le loyer** : c'est un remboursement, jamais
  compté comme un profit de trading (il est affiché à part, `rent_recovered`).
  Les frais de la transaction de fermeture, eux, sont un vrai coût : ils entrent
  dans le PnL.
- **Aucun token laissé sans surveillance** — tout token détenu sans position
  (crash, nœud capricieux) est **adopté au démarrage** et repasse sous TP/SL.
- **RPC avec secours** — si le nœud principal échoue sur une lecture, le bot
  bascule automatiquement sur le **RPC de secours** et l'annonce dans le journal.
  Les connexions HTTPS sont **mutualisées** (mesuré sur le même nœud : ~610 ms par
  appel avec un handshake neuf, ~300 ms en réutilisant la connexion — soit
  ~310 ms gagnés à chaque appel) et plusieurs lectures partent dans un
  **même aller-retour** : trois appels au prix d'un.
  L'état du nœud (succès, échecs, latence médiane, WebSocket) est affiché dans le
  panneau *Configuration de trading*.
- **Dashboard** moderne (données live via WebSocket).

---

## Critères de snipe (tous configurables)

Un token est **acheté** uniquement si **tous** les critères passent :

| Critère | Règle | Paramètre |
|---|---|---|
| Top 10 holders | `< 30 %` | `max_top10_percent` |
| MarketCap vs Volume | `MC < Volume` | `require_mc_gt_volume` |
| Acheteurs / Vendeurs | `acheteurs > vendeurs` | `require_more_buyers` |
| Description / Site / Réseaux | présence exigée | `require_website`, `require_social`, `require_description` |
| Engagement social réel | pas de handles dupliqués en masse | `require_social_engagement`, `max_social_dup` |
| KOLs | `> 0` | `require_kol` |
| Smart wallets | `> 0` | `require_smart_wallet` |
| MarketCap **ATH** max | le token n'a **jamais** dépassé `40 000 $` (ATH market cap) | `max_market_cap` |
| Âge max | `≤ 15 min` | `max_age_minutes` |
| **Coût des frais** | l'aller-retour (base ×3 + priority ×2) coûte **≤ 3 %** du trade | `max_fee_percent` |

Tout critère violé ⇒ **rejet** (avec la raison exacte affichée dans le journal).

> **Coût des frais** : les frais sont un montant **fixe** en SOL, pas un
> pourcentage — leur poids grandit donc quand le trade rétrécit. Mesuré sur les
> transactions réelles de ce bot (frais lus on-chain, `getTransaction`) :
> **2,35 % à 15,58 %** d'un trade de 0,001 SOL, médiane **10,88 %**, dont
> 9 648 à 105 000 lamports pour le seul achat. Avec un TP à 15 %, un trade qui
> paie 10 % de frais doit gagner bien plus souvent qu'il ne perd pour seulement
> rentrer dans ses frais. Le critère refuse donc un trade qui ne peut pas payer
> ses propres frais ; `0` désactive la règle explicitement. Le panneau
> *Configuration de Trading* affiche le coût estimé en direct.

> **MarketCap ATH** : le plafond de 40 000 $ porte sur l'**ATH** (plus haut
> historique), pas sur la valeur du moment. Un token qui a déjà atteint 40 000 $
> puis est retombé est **rejeté**. La valeur ATH est calculée depuis
> `ath_price × circulating_supply` fourni par GMGN ; si l'ATH est indisponible,
> le market cap actuel est utilisé en repli (le filtre devient au pire plus
> strict, jamais plus permissif).

> Les critères sont modifiables en direct dans **Settings** et dans le panneau
> *Configuration de Trading* du dashboard.

> **Ce que valent les critères, mesuré** — sur une fenêtre d'environ 1 h et
> ~254 alertes réelles, seuls `max_market_cap` et `max_top10_percent` séparaient
> les tokens qui montaient des autres. `mc_vs_volume`, `buyers`, `links`,
> `social_engagement`, `kol` et `smart` étaient **inversés** dans cette fenêtre
> (les tokens qui les passaient faisaient moins bien que la médiane), et **3 %**
> seulement des tokens cotés passaient au-dessus de leur prix d'appel. Chaque
> interrupteur `require_*` peut donc être éteint sans redéployer. Mesure
> **marginale par critère**, sur **une seule fenêtre**, et qui **exclut** les
> tokens sans cotation Dexscreener : à refaire avant d'en tirer une règle.

---

## Un seul achat par token, et un PnL de clôture qui ne bouge plus

Les trois canaux republient volontiers le même contrat, et un token peut être
appelé plusieurs fois à des heures d'écart. Le bot n'achète donc **qu'une fois
par contrat**, quel que soit le chemin — canal Telegram, snipe automatique ou
bouton manuel : dès qu'une position existe pour une adresse — **ouverte ou déjà
fermée** — un nouvel appel est refusé, et le journal le dit.

| Situation | Ce que fait le bot |
|---|---|
| Appel Telegram d'un contrat déjà acheté | refusé **avant** la file d'analyse (aucune requête GMGN dépensée) : `⏭️ <token> déjà traité (trade clôturé) — un seul achat par token, appel ignoré` |
| Snipe automatique du même contrat | aucun second achat ; l'appel est enregistré **non exécuté** avec la raison « Token déjà traité » |
| Contrat **différent** portant le **même nom** | acheté normalement : la règle porte sur l'adresse, jamais sur le nom |
| Token **rejeté** la première fois | reste snipable plus tard : la règle porte sur les positions, pas sur les alertes |
| Snipe **manuel** (bouton) | refusé comme les autres, **avant** l'analyse (aucune requête GMGN) : `⏭️ <token> déjà traité (position ouverte) — un seul achat par token, snipe manuel refusé`. Deux clics sur le même contrat pendant que le premier tourne : le second est refusé (`Snipe manuel déjà en cours`) et le bouton est désactivé le temps de la requête, car le clic contourne la file d'analyse |
| Après un **redémarrage** | la règle tient toujours — elle lit les positions enregistrées, là où la déduplication de 30 minutes n'est qu'une mémoire vive |

Le snipe manuel garde **un seul** privilège : il ne dépend pas de l'interrupteur
*Sniper automatique*. Il obéit à toutes les autres règles, celle-ci comprise — un
clic n'est pas un rachat implicite.

Le réglage **`snipe_once_per_token`** (Settings, **actif** par défaut) permet de
revenir au comportement précédent.

> **Ce que « le même token » ne veut pas dire.** Les données réelles sont sans
> ambiguïté : **83 positions, 83 adresses distinctes** au moment du diagnostic
> (et **103 / 103** quelques heures plus tard), **aucun rachat** — jamais. La
> règle a d'ailleurs refusé son premier appel en production : `COMPUTE`, appelé
> une seconde fois après la clôture de son trade, a été écarté —
> `⏭️ COMPUTE déjà traité (trade clôturé) — un seul achat par token, appel
> ignoré` — sans créer de position. Ce qui ressemblait à un triple achat, ce sont
> trois **contrats différents** publiés sous le même nom : `GM` apparaît 3 fois,
> « Songgang » 2 fois, plus une variante coréenne `송강`. L'ancien affichage
> aggravait la confusion : tous les contrats pump.fun finissent par `pump`, donc
> `shortAddr()` rendait `7ZY...ump` et `FTM...ump` — trois caractères de tête,
> une queue identique pour tous. L'adresse s'affiche maintenant `7ZYkyb…53bv` /
> `FTMtRC…D7ux` (six caractères de tête et, pour ces contrats, les quatre
> caractères **avant** le suffixe constant), et l'adresse complète est en
> infobulle.

### Le journal n'écrit plus deux lignes d'achat par achat

Un achat papier produisait **deux** lignes portant l'action d'achat : la ligne
d'achat, puis la note « simulation refusée faute de SOL ». Lu de haut en bas, le
même token semblait acheté deux fois. La note est maintenant la **suite du même
achat** (`↳ même achat : …`, action `note`), et un achat n'écrit plus qu'**une
seule** ligne d'action `buy`.

### Le PnL d'un trade fermé est figé

Une position fermée conserve le PnL réalisé, le prix de sortie et la raison de
sortie enregistrés à la clôture. Deux protections, et non une seule : **le suivi
ne réévalue que les positions ouvertes** (`store.open_positions()`), et
`_revalue` refuse de son côté tout enregistrement déjà fermé — l'invariant tient
donc même pour un appelant qui passerait la mauvaise liste. La ligne s'affiche
`Fermée · <raison>` avec le prix de sortie. Preuve exécutable dans
`tools/verify_once.py` : le contrôle inverse reprend **le même enregistrement**,
ne change que son statut en `open`, et montre que le tick réécrirait alors bel et
bien le PnL — c'est donc le statut qui gèle le chiffre, pas le hasard des valeurs.

---

## Fermer une position à la main

Deux endroits, la même boîte de dialogue :

- la **croix ✕** dans le panneau « Positions ouvertes » du tableau de bord ;
- le bouton **« Fermer »** dans la ligne du tableau **Toutes les positions**.

Un clic **ne vend plus rien** : il ouvre une confirmation qui rappelle le token,
son adresse, le PnL suivi, et propose **25 %, 50 % ou 100 %** — 100 % par défaut.
C'est le bouton de la boîte qui vend, et lui seul.

| Choix | Ce qui se passe |
|---|---|
| **25 %** ou **50 %** | la position reste **ouverte** pour le reste ; les jetons, le coût engagé et le montant en SOL sont réduits d'autant, le produit de la vente est mis de côté, et **le PnL continue de suivre le prix** |
| **100 %** | la position est **fermée**, son PnL réalisé est figé, et le produit enregistré est la **somme de toutes les ventes** — y compris les sorties partielles précédentes |

La boîte dit aussi, en clair, ce que l'acte engendre : sur une position **papier**
la vente est cotée sur le marché réel (même route, même prix) mais **aucun ordre
n'est signé** ; sur une position **réelle**, la vente signe et diffuse une
transaction, et le bouton de confirmation passe au **rouge**.

**Un refus s'affiche là où le clic a eu lieu.** Le journal a montré un vrai cas de
vente impossible sur un **HTTP 429 de Jupiter** (« Too many requests ») : la
position restait ouverte sans que rien ne le dise. Désormais le motif s'écrit dans
la boîte, suivi de « La position est inchangée. », et la position reste
effectivement intacte — statut, jetons et PnL compris. Une position déjà fermée
est refusée explicitement (« Position déjà fermée (take_profit) ») au lieu de
partir en devis et de revenir avec un calcul d'arrondi.

> **Mesuré en production, sur une vraie position papier** (`Booty`, ouverte à
> 3 501 387 jetons) : vente de 50 % depuis la boîte → statut toujours `open`,
> **1 750 693 jetons** (exactement la moitié), `cost_usd` 10,582 → 5,291,
> `cost_usd_total` et `tokens_bought` **conservés**, produit 0,048232 SOL mis de
> côté. Puis 100 % du reste → `closed`, `tokens: 0`, `proceeds_sol: 0,0964641`
> (**deux fois 0,048232 — les deux ventes, pas seulement la dernière**),
> `exit_reason: manual`, PnL réalisé −0,3743 $ (−3,54 %), frais 0,00008 SOL inclus.
>
> **Et le même geste, refait depuis l'écran après la correction du clic**
> (`Return`, 3 500 518 jetons) : la croix ouvre enfin la boîte, **25 %** vendus,
> statut toujours `open`, **2 625 389 jetons** (exactement 75 %), `cost_usd`
> 10,397 → 7,798, `proceeds_sol` 0,02419302, frais de vente 0,000025 SOL — aucun
> message d'erreur, aucun échec console. Le contrôle du clic avait été fait
> *avant* sur la même page : boîte ouverte depuis la croix **et** depuis le bouton
> « Fermer », annulation propre, et un clic sur la ligne continue de charger le
> graphique du token.

Quatre défauts de fond ont été trouvés en construisant et en éprouvant ceci :

- **le produit des ventes partielles n'était pas enregistré à la clôture** —
  `_close_position` l'utilisait correctement pour calculer le PnL, mais ne
  l'écrivait pas dans l'enregistrement fermé, qui affichait donc le produit de la
  dernière vente partielle seulement ;
- **`_revalue` ne se protégeait pas lui-même** : il fallait que l'appelant lui
  passe uniquement des positions ouvertes. Le refus est maintenant aussi dans la
  fonction (voir plus haut).

**Et le clic lui-même n'arrivait pas.** Le bouton « Fermer » est rendu *dans* une
ligne `<tr data-select>` : la ligne entière est cliquable pour charger le
graphique du token. Le délégué de clic parcourt ses sélecteurs **dans l'ordre du
code** ; `[data-select]` était testé *avant* `[data-close]`, donc un clic sur la
croix ✕ ne fermait rien — il changeait simplement le token sélectionné, sans un
mot, sans dialogue, sans notification. Deux fois le même piège : « Fermer »
et « TP/SL ». Les deux boutons sont maintenant testés **avant** la ligne
cliquable, et un contrôle vérifie l'ordre du code, pas seulement l'existence des
boutons — parce que c'est l'ordre qui fait marcher la chose.

**Une position qu'on ne voit pas ne peut pas être fermée.** Le tableau de bord ne
reçoit qu'un instantané plafonné à **60 positions**. Avec 34 positions ouvertes
sur 139, les 60 plus récentes ne contenaient que 11 ouvertes : **23 positions
ouvertes n'existaient pas pour le navigateur**, donc aucun bouton pour les
fermer, alors que le bot, lui, les gérait toujours. L'instantané place désormais
**toutes** les positions ouvertes en tête, puis complète avec l'historique des
clôturées jusqu'au plafond. Le plafond ne rogne plus que l'historique, et le
capital engagé affiché — calculé sur les mêmes données — devient exact lui aussi.

---

## Mode papier (`paper_trading`)

L'interrupteur **Papier / Réel** dans la barre du haut, le réglage
**Settings → Mode papier (aucun ordre réel)**, ou `"paper_trading": true` dans
`data/settings.json`. Quand il est actif :

- **Rien n'est signé, rien n'est diffusé.** La clé privée n'est même pas lue : le
  wallet, le plan GMGN et le solde n'entrent plus dans la décision. Le mode papier
  fonctionne donc **sans wallet et sans fonds**.
- **Les données sont réelles.** Le call Telegram, l'analyse GMGN, les prix
  Dexscreener et les K-lines sont ceux du marché. Rien n'est simulé de ce côté.
- **Le prix d'entrée est le prix réellement exécutable.** Le bot demande à Jupiter
  un devis **pour la taille exacte du trade** et prend le montant que le pool
  donnerait vraiment — l'**impact de prix** est donc inclus, contrairement au prix
  affiché par Dexscreener.
- **L'achat est réellement simulé.** Le swap est construit puis passé à
  `simulateTransaction` : une route que la chaîne refuserait à cet instant fait
  **échouer** le trade papier, exactement comme un ordre réel aurait payé ses frais
  pour rien. Le mode papier ne peut donc pas embellir une stratégie qui ne
  passerait pas. Deux cas sont distingués explicitement :
  - **route refusée** (erreur du programme) → l'achat papier est refusé ;
  - **fonds insuffisants** (l'enveloppe WSOL + le compte de token demandent plus
    que le solde) → ce n'est pas la faute de la route : l'achat papier continue,
    mais le remplissage est marqué *route non validée par la simulation* dans la
    position et dans le journal. En mode réel, ce cas lève immédiatement
    « Fonds insuffisants … rien n'a été envoyé » au lieu de réessayer trois routes
    inutilement.
- **Les sorties sont cotées pour de vrai — jamais simulées.** TP, SL, trailing
  stop et ventes manuelles demandent à Jupiter le prix d'une sortie **réelle** de
  la quantité détenue (impact de prix inclus). La simulation est volontairement
  omise : le wallet ne détient pas les tokens virtuels, donc le nœud refuserait
  forcément — la simulation ne dirait rien du marché. Le **devis** suffit à la
  question posée : une route existe-t-elle, et à quel prix ? Un token devenu
  invendable échoue donc à la vente **ici aussi** — c'est précisément le risque que
  ce mode existe pour mesurer. Une vente papier impossible laisse la position
  ouverte et l'indique dans le journal.
- **Les frais sont comptés au même barème** que le critère « Coût des frais »
  (`base ×3 + priority ×2`, soit `buy_fee_sol + sell_fee_sol + close_fee_sol`), donc
  un PnL papier et un PnL réel sont **directement comparables**. C'est une borne
  **haute** : les frais de priorité sont rarement consommés en totalité, donc un
  résultat réel sera égal ou légèrement meilleur.
- **Les résultats papier ne sont jamais mélangés aux résultats réels.** Position
  par position (`paper: true`), les statistiques ont un tableau de bord séparé
  (`stats.paper`) ; les positions papier portent un badge `PAPIER`, l'en-tête
  affiche « **Mode papier — aucun ordre réel** », et `live_trading` est ignoré.
- **La limite de positions simultanées est par mode** : un test papier n'est pas
  freiné par une position réelle, et inversement.

### Deux règles de sécurité, dans les deux sens

Le mode est une propriété de **chaque position**, jamais du réglage courant :

| Situation | Comportement |
|---|---|
| Mode papier **activé** alors qu'une position **réelle** est ouverte | Elle est **vendue pour de vrai**. Aucun bag réel n'est jamais abandonné. |
| Mode papier **désactivé** alors qu'une position **papier** est ouverte | Elle est **fermée en virtuel** (elle ne possède aucun token on-chain). |

### Ce que le mode papier ne mesure pas

- **La latence de soumission et le remplissage réel d'un bloc.** Le devis et la
  simulation sont pris à l'instant du trade, mais un ordre réel part avec le même
  devis une fraction de seconde plus tard : en pratique le résultat réel est
  **légèrement moins bon** que le papier, jamais meilleur.
- **La validation de la route à la vente** (voir ci-dessus) : une vente papier est
  cotée, pas simulée. Un gain papier n'est donc pas la preuve qu'on aurait pu
  sortir au même prix.
- **Le solde du wallet** : tant que le solde couvre un trade, l'achat papier est
  validé par une vraie simulation. S'il ne le couvre pas (enveloppe WSOL + compte
  de token ≈ 0,0056 SOL pour un achat de 0,002 SOL), le remplissage reste coté mais
  marqué comme non validé — le mode papier fonctionne donc **sans aucun fonds**.

### Un token que plus personne ne peut vendre

Quand un pool se vide, Jupiter ne renvoie plus aucune route : `No routes found`,
quoi qu'on demande. La règle de sortie (SL, trailing) continue de se déclencher à
chaque tick, donc le bot demandait une vente **toutes les 5 secondes, pour
toujours**. Mesuré sur `SOOTS` : **34 ventes ratées** identiques, puis un journal
illisible.

La règle est maintenant explicite, et elle ne s'applique **qu'au papier** :

| Étape | Comportement |
|---|---|
| **Règle automatique**, 1 à 3 échecs | la position reste **ouverte**, le compteur est écrit sur la position (`no_route_failures`) et la ligne du tableau affiche `aucune route ×n` |
| **Règle automatique**, 4<sup>e</sup> échec | la position est **soldée une seule fois** au **dernier prix Dexscreener connu** ; `exit_unpriced: true`, `exit_price_source: dexscreener`, note `route de vente inexistante — prix non exécutable`, et la ligne porte le badge **`prix non exécutable`** |
| **Un clic sur « Fermer » (totalité)** | **une seule fois suffit** : la position est soldée tout de suite, au même prix étiqueté. Un clic est une décision, pas une réessai — attendre quatre refus identiques donnait l'impression d'un bouton cassé |
| **Un clic sur une vente partielle** | **refusé**, avec la consigne : seul le sac entier peut être soldé au dernier prix connu. Aucun compteur n'est avancé |
| **Panne passagère** (429, timeout) | **ne compte pas** : elle ne rapproche jamais d'une liquidation, et le message dit que Jupiter n'a pas répondu (réessayez) au lieu de laisser croire que le marché est vide |
| **Position réelle** | **jamais soldée** à un prix inventé. Elle continue d'échouer franchement jusqu'à ce qu'un marché existe. |
| **La route revient** | le compteur repart à zéro et la vente se fait normalement, sans badge. |

Ce n'est pas une vente : c'est une **sortie de portefeuille comptable**, pour que
le livre n'affiche pas éternellement un sac invendable. Le PnL est une estimation
de ce que le sac valait, et c'est écrit noir sur blanc dans la position, dans le
journal et dans l'infobulle du badge — jamais présenté comme de l'argent reçu.

> **Mesuré en production** : `SOOTS`, sortie `trailing_stop` décidée, quatre
> tentatives comptées (`1/4`, `2/4`, `3/4`, puis la liquidation), une seule ligne
> de liquidation au dernier prix `$0,0000000806`, PnL **−14,22 %** (−1,4309 $),
> produit 0,08613247 SOL, frais 0,000055 SOL. Badge `prix non exécutable` affiché
> dans **Toutes les positions**, 0 erreur console. Plus aucune requête Jupiter
> pour ce token.

---

## Basculer papier / réel depuis le tableau de bord

L'interrupteur **Papier / Réel** est dans la barre du haut, toujours visible : la
phrase « aucun ordre réel n'est envoyé » ne doit jamais demander un clic pour être
vérifiée. Il nomme **trois** états, parce que deux d'entre eux se confondent
facilement :

| État | `paper_trading` | `live_trading` | Ce qui se passe |
|---|---|---|---|
| **PAPIER** (vert) | ✔ | — | Rien n'est signé ni diffusé. `live_trading` est ignoré. |
| **RÉEL** (rouge) | ✘ | ✔ | Les snipes engagent de vrais SOL. |
| **OBSERVATION** (ambre) | ✘ | ✘ | Analyses et prix réels, **aucun ordre** : appeler ça « réel » serait un mensonge que le wallet paierait. |

- **Passer en réel demande confirmation.** La fenêtre rappelle que les prochains
  snipes engagent de vrais SOL, affiche le **montant par trade** et le **solde réel
  du wallet** (si le solde est sous le montant, elle le dit avant, pas après un
  refus). Elle ne coche jamais « Trading réel » à votre place : la case est là,
  cochée par défaut, mais c'est **vous** qui l'armez — un clic isolé peut donc
  laisser le bot en observation, jamais dans le marché.
- **Revenir au papier est immédiat**, sans confirmation : ce sens ne fait que
  retirer la capacité d'envoyer un ordre, et un frein qui exige deux clics est un
  frein qu'on apprend à ne pas utiliser. Le message rappelle que les positions
  **réelles** déjà ouvertes seront, elles, toujours vendues pour de vrai.
- **Le serveur est la seule autorité.** `POST /api/mode` valide la demande
  (`400` pour autre chose que `paper` / `live`), applique, trace le changement avec
  son **origine** (jamais un secret), puis répond avec l'état **réellement atteint**
  — y compris quand il a fait *moins* que demandé. Le mode est annoncé dans
  `/api/state`, dans l'instantané WebSocket et dans `/api/health` (`trading`), donc
  l'en-tête, le bandeau papier et le panneau de réglages ne peuvent pas se
  contredire.
- **Les deux interrupteurs restent synchronisés** : le réglage de `Settings` porte
  sur les mêmes deux booléens, et l'état est recalculé au même endroit dans le
  navigateur (`modeInfo()`).

`tools\verify_mode.py` prouve les trois points qui comptent : les **trois** états sont
nommés correctement (les libellés du serveur existent tels quels dans l'interface),
**aucun clic n'arme le trading réel tout seul** (le patch réellement construit est
inspecté), et **basculer ne change jamais la nature d'une position ouverte** (une
position papier reste virtuelle, une position réelle reste vendue pour de vrai). Il
éprouve l'endpoint avec des doubles — `update`, `log` et `broadcast` sont remplacés —
donc `data/settings.json` n'est pas touché (vérifié octet pour octet).

```powershell
.\venv\Scripts\python.exe tools\verify_mode.py
```

---

## Prérequis

- **Windows** (testé), PowerShell
- **Python 3.11+**
- **Node.js 18+** et le CLI GMGN : `npm install -g gmgn-cli`
- Un **compte Telegram** (api_id / api_hash / numéro)
- Une **clé API GMGN** : https://gmgn.ai/ai
- ⚠️ **Un plan GMGN payant (Plus ou Pro) n'est nécessaire que pour l'exécution
  `gmgn`.** Les routes de trading (`order quote`, `trade/swap`) coûtent ~11 de
  poids de rate-limit, alors que le plan **Free** n'a qu'une capacité de 5 : GMGN
  répond alors `403 PLAN_UPGRADE_REQUIRED`. C'est exactement pour cela que le
  moteur d'exécution par défaut est **Jupiter** : la signature est faite
  localement, donc **aucun plan GMGN n'est requis pour acheter ou vendre**.
  Les lectures GMGN (token info, security, K-line, holders) fonctionnent sur le
  plan Free et restent la source de toutes les analyses.

---

## Installation

Dépôt public : <https://github.com/MakTheGeek/trybot>.

```powershell
# 0) Récupérer le code (le dossier de destination est libre)
git clone https://github.com/MakTheGeek/trybot.git TryBot
cd TryBot

# 1) Dépendances Python
python -m venv venv
.\venv\Scripts\python.exe -m pip install -r requirements.txt

# 2) CLI GMGN (fournit les données on-chain)
npm install -g gmgn-cli

# 3) Vos identifiants Telegram, jamais dans le code
Copy-Item .env.example .env
notepad .env
```

`.env` est ignoré par git : c'est là que vivent `TELEGRAM_API_ID`,
`TELEGRAM_API_HASH` et `TELEGRAM_PHONE` (voir *Configuration Telegram* plus bas).
Aucun identifiant n'est écrit en dur dans le code, et `data/` (session Telegram,
carnet de trades, journal) n'est jamais publié non plus.

### Configuration GMGN — `~/.config/gmgn/.env`

```ini
GMGN_API_KEY=gmgn_xxxxxxxxxxxxxxxxxxxxxxxxxxxx
GMGN_PRIVATE_KEY="-----BEGIN PRIVATE KEY-----\n...\n-----END PRIVATE KEY-----"
```

`GMGN_PRIVATE_KEY` est la **clé de signature des requêtes** (pas une clé de
wallet blockchain). Elle n'est utilisée que par le moteur d'exécution `gmgn`.

### Configuration du wallet d'exécution — `~/.config/solsnipbot/wallet.key`

Pour le moteur d'exécution **Jupiter**, le bot a besoin de la clé privée du
wallet Solana qui détient les fonds (format base58, ou tableau JSON de 64
octets). Elle se place dans un fichier **hors du projet** :

```powershell
New-Item -ItemType Directory -Force "$env:USERPROFILE\.config\solsnipbot" | Out-Null
Set-Content "$env:USERPROFILE\.config\solsnipbot\wallet.key" "VOTRE_CLE_BASE58" -NoNewline
```

L'adresse dérivée de cette clé est affichée dans le tableau de bord ; l'achat est
**refusé** si elle ne correspond pas au wallet suivi (aucun fonds ne peut partir
d'un autre compte). La variable d'environnement `SOLANA_WALLET_PRIVATE_KEY`
fonctionne aussi et a la priorité.

> ⚠️ Une clé privée partagée (chat, capture, dépôt git) doit être considérée
> comme compromise. Pour de vrais montants, créez un wallet neuf, transférez-y
> les fonds, et ne mettez cette clé que dans ce fichier local.

### Configurer les clés depuis le tableau de bord

Plutôt que d'éditer des fichiers à la main, **toutes les clés se règlent depuis
l'onglet Settings**, panneau *Clés & secrets* : clé privée du wallet, clé API
GMGN, clé privée de signature GMGN, clé API Helius et les trois identifiants
Telegram. Chaque ligne indique si la valeur est définie, **d'où elle vient**
(variable d'environnement, fichier du tableau de bord, ancien `.env`, valeur en
dur dans le code, `wallet.key`) et un aperçu masqué.

| | |
|---|---|
| **Stockage** | `~/.config/solsnipbot/secrets.env`, **hors du dossier du bot** : une copie ou une archive du projet ne peut jamais emporter une clé privée. Droits restreints (0600, ACL Windows), réécriture atomique, aucune écriture dans `data/`. |
| **Priorité de lecture** | variable d'environnement → fichier du tableau de bord → anciens `.env`. Une variable d'environnement garde donc toujours le dernier mot, et l'interface **le dit** au lieu de laisser croire que la valeur enregistrée est active. |
| **Ce qui sort de l'API** | jamais la valeur : un masque (`gmgn…6789` ou `PEM (119 caractères)`), la longueur et la provenance. |
| **Validation avant écriture** | une valeur inutilisable est **refusée**, pas enregistrée. La clé du wallet est dérivée sur place : l'adresse obtenue est affichée, et une clé illisible ne peut pas remplacer une clé qui signe. |
| **Prise en compte immédiate** | enregistrer la clé du wallet re-dérive l'adresse et le solde ; enregistrer une clé Helius (ou l'URL RPC) relance la connexion RPC et le WebSocket, sans redémarrer le bot. |
| **Vérification** | le bouton *Tester* dérive l'adresse du wallet, ou interroge le nœud Helius (`getSlot`). Rien n'est signé ni envoyé. |
| **Helius** | enregistrer la clé réécrit l'URL RPC en `https://mainnet.helius-rpc.com/?api-key={api_key}` : la clé cesse d'être écrite en clair dans `data/settings.json`. |
| **Accès en écriture** | accepté depuis toute machine pouvant atteindre le tableau de bord (votre choix) ; la case `secrets_local_only` le limite à la machine locale et chaque écriture est journalisée avec son adresse d'origine (jamais la valeur). |

Les variables d'environnement et les fichiers décrits ci-dessus restent
parfaitement valables : ils gardent la priorité, et rien n'est migré
automatiquement. Le harnais `tools/verify_secrets.py` rejoue toute cette
logique (catalogue, priorité, validations, fichier, masquage) et interroge
l'API réelle si le serveur tourne.

### Configuration Telegram — `.env` (racine du projet)

```ini
TELEGRAM_API_ID=            # votre identifiant, depuis my.telegram.org
TELEGRAM_API_HASH=          # votre hash, jamais publié
TELEGRAM_PHONE=+33...       # le numéro du compte qui écoute
```

`.env` est ignoré par git (`.gitignore`) : c'est le seul endroit où ces valeurs
doivent vivre. Le code ne contient aucun identifiant en dur — un dépôt public ne
doit jamais révéler un API hash, car il identifie l'application elle-même.

### Skills GMGN pour OpenCode

Les 19 skills GMGN (recherche de token, marché/K-line, portefeuille, swap,
détection de call-outs, scoring wallet/dev…) sont installées pour OpenCode :

```powershell
git clone https://github.com/GMGNAI/gmgn-skills $env:USERPROFILE\.opencode\gmgn-cli
New-Item -ItemType Junction -Path $env:USERPROFILE\.config\opencode\skills\gmgn-cli `
         -Target $env:USERPROFILE\.opencode\gmgn-cli\skills
```

OpenCode découvre alors `gmgn-token`, `gmgn-market`, `gmgn-portfolio`,
`gmgn-swap`, `gmgn-track`, `gmgn-callout`, `gmgn-contract-dd`, etc.
Les identifiants sont lus depuis `~/.config/gmgn/.env` (aucune copie du secret).

---

## Lancement

```powershell
.\start.bat
# ou
.\venv\Scripts\python.exe run.py
```

Puis ouvrez **http://127.0.0.1:8787**

### Auto-contrôle

```powershell
.\venv\Scripts\python.exe tools\selfcheck.py
```

Vérifie la chaîne complète sans passer d'ordre : TLS/RPC, clé et wallet, tous
les paramètres, les filtres (sur les analyses déjà enregistrées **et** sur une
analyse fraîche), TP/SL/trailing (11 scénarios), les montants de vente, le PnL
net de frais, le **mode papier** (aucun ordre ne part, une position réelle reste
réelle, une position papier reste virtuelle, une vente partielle n'écrase pas le
capital de référence), le **suivi de prix continu** (ce qui est suivi, ce qui est
libéré, une requête Dexscreener réelle et groupée), puis un devis Jupiter signé
localement et **simulé** sur le RPC.

Le carnet de positions est mis de côté le temps de la course puis remis tel
quel, comme la liste des appels : une position papier laissée ouverte par une
session en cours ne peut donc pas faire échouer un contrôle qui compte
« exactement une position ». Le rapport ne s'éclaire qu'en rouge pour une raison
qui vous concerne.

### Vérification paramètre par paramètre

```powershell
.\venv\Scripts\python.exe tools\verify_params.py
```

Répond à une question différente : **chacun des réglages change-t-il vraiment
le comportement du code qui l'utilise ?** Enregistrer une valeur ne prouve rien.
Le harnais cherche donc, pour chaque clé, le module qui la consomme, la réécrit
puis la relit (dans un fichier de réglages *temporaire*, jamais le vrai), éprouve
les bornes de validation une par une, puis pilote les vraies fonctions avec des
doubles (devis, construction, simulation, GMGN, Dexscreener) :

| Ce qui est prouvé | Comment |
| --- | --- |
| **Montant du trade** | un achat papier de 0,1 SOL engage exactement 0,1 SOL ; un achat réel construit exactement `round(montant × 1e9)` lamports, et le solde requis (montant + 0,003 de réserve) est comparé **au lamport près** |
| **TP / SL / trailing** | la valeur réglée est posée sur la position, puis la boucle de gestion réelle (`_revalue`) sort au bon seuil — et pas avant |
| **trailing_enabled** | le même parcours en repli ne déclenche rien quand la règle est éteinte |
| **Les 10 filtres** | un token synthétique conforme est retenu ; chaque entrée est ensuite poussée de part et d'autre de son seuil, et chaque interrupteur `require_*` est éteint pour vérifier qu'il neutralise bien son critère. Les sondes **forcent** l'interrupteur qu'elles éprouvent au lieu de lire la configuration du moment : éteindre un critère dans le tableau de bord ne fait pas échouer la suite |
| **`max_market_cap`** | un token retombé sous 40 k mais dont l'ATH a dépassé 40 k est **refusé** |
| **`max_age_minutes`**, **`buyer_seller_window`** | les autres fenêtres lisent d'autres compteurs, et le résultat change |
| **Pré-filtre d'alerte** | les 3 plafonds, la marge anti-arrondi, et l'âge qui vieillit en file d'attente |
| **`live_trading` / `paper_trading` / `auto_snipe` / `max_concurrent_positions`** | l'achat part ou ne part pas selon le réglage ; une position papier ne bloque pas un ordre réel |
| **`slippage` / `priority_fee` / `anti_mev`** | les valeurs arrivent dans l'ordre réellement construit, sur Jupiter **et** sur GMGN |
| **`max_fee_percent`** | un trade trop petit pour payer ses frais est refusé ; `0` désactive explicitement |

Rien n'est envoyé sur la chaîne : `sign_and_send` lève une exception si on
l'appelle, et aucune écriture ne touche `data/`.

### Vérification des clés et secrets

```powershell
.\venv\Scripts\python.exe tools\verify_secrets.py
```

Prouve que le panneau *Clés & secrets* fait vraiment quelque chose, et qu'il ne
laisse rien fuir : chaque secret du catalogue est relié au module qui le
consomme ; la priorité de lecture (environnement → fichier du tableau de bord →
anciens `.env`) est rejouée couche par couche ; chaque validation est éprouvée
dans les deux sens (clé de wallet dérivée, PEM tronqué, API hash de 31
caractères, numéro mal formé…) ; le fichier est réécrit puis relu (PEM
multiligne, valeur contenant `=`, clé inconnue conservée, pas de BOM) ; et
**aucune valeur en clair n'apparaît** dans ce que l'API renvoie. Ses essais
d'écriture visent un dossier temporaire : le fichier réel est contrôlé
inchangé en début et en fin de course.

Un dernier contrôle vise l'instant qui suit l'enregistrement : les symboles que
l'API appelle alors (re-dérivation du wallet, redémarrage du socket RPC,
réécriture de l'URL Helius) sont confrontés aux modules réels. Un renommage est
ainsi attrapé par le harnais, et non par un message d'erreur au milieu du
tableau de bord.

### Vérification de la bascule papier / réel

```powershell
.\venv\Scripts\python.exe tools\verify_mode.py
```

Prouve les trois points qui valent de l'argent : les **trois** états (papier, réel,
observation) sont nommés correctement — les libellés du serveur doivent exister
**tels quels** dans `frontend/app.js`, donc un renommage d'un côté est attrapé par
l'autre ; **aucun clic n'arme le trading réel tout seul**, ce qui est vérifié sur
le patch réellement construit (`live_trading` n'y apparaît que si la case a été
cochée) et non sur le texte de l'interface ; et **basculer ne change jamais la
nature d'une position ouverte**. L'endpoint est éprouvé avec des doubles
(`update`, `log`, `broadcast` remplacés), donc `data/settings.json` est contrôlé
**inchangé octet pour octet** ; si le serveur tourne, la dernière section
l'interroge en lecture et en refus seulement — jamais sur un succès, qui écrirait
un vrai changement de mode sur le bot en service.

### Vérification de la chaîne de sortie (TP / SL / trailing)

```powershell
.\venv\Scripts\python.exe tools\verify_exits.py
```

Le seul harnais qui éprouve `_confirm_exit` **pour de vrai**. `verify_params.py`
pilote bien la boucle de gestion, mais il **remplace** ce contrôle par un double
qui rend toujours la même raison : il prouve donc le seuil, et jamais le contrôle
du prix exécutable — précisément le maillon qui annule un Take Profit dont le prix
affiché n'est pas un prix que le marché paie. Les deux suites étaient vertes
pendant qu'un TP réglé à +15 % ne se déclenchait pas alors que l'affichage
atteignait +39 %.

| Ce qui est prouvé | Comment |
| --- | --- |
| **Bornes exactes** | TP à +15 % déclenché à +15,00 % et pas à +14,99 % ; idem SL en négatif ; `0` **désactive** au lieu de déclencher |
| **Priorité** | Take Profit et Stop Loss priment sur le trailing ; le trailing suit le pic, pas l'entrée |
| **TP exécutable** | affiché +20 % / exécutable +18 % ⇒ honoré ; affiché +60 % / exécutable −2 % ⇒ **annulé** ; exécutable juste sous le seuil ⇒ annulé |
| **Stop jamais affaibli** | un écart de 24 % (sous la tolérance de 25 %) conserve la décision ; au-delà elle est recalculée ; un marché **pire** que l'affichage déclenche quand même |
| **Devis indisponible** | la décision est maintenue, la protection n'est jamais désactivée |
| **Le bon montant est coté** | position papier : ses propres unités ; position réelle : le solde on-chain, lu et non inventé |
| **Boucle complète, papier** | le faux pic observé : rien n'est vendu, la position reste ouverte, l'annulation est journalisée avec les deux prix |
| **Écart porté à l'écran** | l'écart mesuré est écrit sur la position pour la ligne (badge + PnL au prix exécutable), puis **effacé** au premier tick sans désaccord |
| **Câblage de l'affichage** | `frontend/app.js` est lu : les deux tableaux appellent bien le badge, et son infobulle nomme le prix affiché, le prix exécutable et la règle appliquée |
| **Boucle complète, réel** | mêmes décisions, et **rien n'est jamais signé** |
| **Montant du trade** | 0,1 SOL réglé ⇒ 0,1 SOL engagé ; 0,05 SOL ⇒ 50 000 000 lamports construits ; 103 000 000 lamports acceptés, 102 999 999 refusés |
| **Invariant de mode** | une position papier se ferme **toujours** virtuellement (même en mode réel), une position réelle se vend **toujours** pour de vrai (même en mode papier) |
| **Pourcentage de vente** | `−5` et `0` sont refusés ; une valeur illisible devient 100 %, jamais 0 |

Aucune écriture dans `data/` : les méthodes du magasin et les appels réseau sont
remplacés, `sign_and_send` lève si on l'appelle, et `data/settings.json` est
contrôlé **identique octet pour octet** en fin de course.

### Vérification de « un seul achat par token » et du PnL de clôture

```powershell
.\venv\Scripts\python.exe tools\verify_once.py
```

Ce harnais est né d'une plainte précise — « le bot snipe le même token 2 fois,
3 fois » — que les données ont démentie : **83 positions, 83 adresses
distinctes**. Deux défauts réels se cachaient derrière l'impression, et un
manque : rien ne garantissait qu'un même contrat ne serait pas racheté plus tard.

| Ce qui est prouvé | Comment |
| --- | --- |
| **La règle** | une position ouverte **et** une position fermée comptent comme « déjà traité » ; un contrat jamais acheté, non ; deux contrats du même nom ne se confondent pas |
| **Refus à l'entrée** | l'appel est refusé **avant** la file (`already-traded`), donc sans requête GMGN ; la file reste vide |
| **Refus à l'achat** | le vrai `_process` n'appelle pas `trader.buy`, enregistre l'appel non exécuté, et écrit la raison |
| **Pas de sur-blocage** | un **autre** contrat nommé pareil est bien acheté ; un token **rejeté** reste snipable le jour où il passe |
| **Snipe manuel** | refusé lui aussi sur un contrat déjà traité, **avant** l'analyse (compteur d'analyses à zéro) ; le bouton répond `already-traded` avec la phrase affichée ; contrôle positif : le même bouton achète bien un contrat jamais traité |
| **Double clic** | deux `force_snipe` **simultanés** sur le même contrat (analyse bloquée sur un jeton de test) : le second rend `in-progress`, **un seul** achat part, et le contrat ressort de l'état « en cours » — un clic plus tard retombe sur la règle, pas sur `in-progress` |
| **Redémarrage** | un `Bot()` neuf (mémoire d'appels vide) refuse toujours : la règle lit les positions enregistrées |
| **Réglage** | `snipe_once_per_token = false` rend l'ancien comportement |
| **PnL figé** | la boucle de gestion ne voit que les positions ouvertes ; le trade fermé ressort **identique, octet pour octet**, et le réalisé du tableau est bien celui-là |
| **Contrôle inverse** | le **même enregistrement**, seul le statut passe à `open` : le tick réécrit alors bel et bien le PnL — c'est donc le statut qui gèle le chiffre, et non le hasard des valeurs |
| **Une seule ligne d'achat** | un vrai `_paper_buy` ne produit qu'**une** ligne d'action `buy` ; la note de financement est une suite (`↳`) d'action `note` ; le remplissage reste marqué « non validé par la simulation » |
| **L'écran** | `shortAddr` distingue les quatre contrats réels du test ; les deux tableaux portent l'adresse complète en infobulle ; une position fermée s'affiche `Fermée · <raison>` avec son prix de sortie |
| **Hermétisme** | `state._write` est remplacé par un compteur : les écritures sont bien traversées (le test n'est pas vide) et **aucun octet** ne part sur le disque ; `data/settings.json` est inchangé |

### Vérification de la fermeture manuelle

```powershell
.\venv\Scripts\python.exe tools\verify_close.py
```

| Ce qui est prouvé | Comment |
| --- | --- |
| **Un clic ne vend pas** | le corps de `closePosition` ne contient **aucun** appel réseau : il ouvre la boîte, un point c'est tout ; la vente part de `#close-confirm` |
| **Les trois parts** | 25 / 50 / 100 sont proposées, 100 % sélectionné au départ, et la phrase comme le libellé du bouton suivent le choix |
| **Vente partielle** | 50 % sur une position papier : statut toujours `open`, jetons et `cost_usd` divisés par deux, `cost_usd_total`/`tokens_bought` conservés, produit et frais de vente mémorisés, PnL toujours suivi |
| **Vente totale** | `closed`, `exit_price`, `exit_reason = manual`, `closed_at`, frais inclus, jetons à zéro, **produit = somme des deux ventes** |
| **PnL figé après coup** | trois ticks de prix sur l'enregistrement fermé : **identique**, et la position a quitté `open_positions()` |
| **Bornes du pourcentage** | `0` et négatif refusés sans rien vendre ; `150` ramené à 100 ; une valeur non numérique vaut 100 et ne provoque pas de vente partielle accidentelle |
| **Papier reste papier** | une position papier se ferme en virtuel **même** avec `paper_trading = false` et **sans wallet** |
| **Réel reste réel** | une position réelle sans wallet est refusée (« Aucun wallet configuré ») et **reste ouverte**, sans PnL écrit |
| **Vente refusée** | un 429 de Jupiter simulé : refus explicite, position **intacte** (statut, jetons, PnL), ligne de journal, et la vente repasse dès que le marché répond |
| **Refus clair** | identifiant inconnu → « Position introuvable » ; position déjà fermée → « Position déjà fermée (take_profit) », **pas** un calcul d'arrondi |
| **La phrase est bien formée** | la phrase de vente partielle est d'un seul tenant (un artefact `" + "` s'était glissé dans le gabarit et s'affichait tel quel) |
| **La croix atteint bien le code** | le bouton vit **dans** une ligne `<tr data-select>` : le contrôle prouve d'abord le piège (les boutons « Fermer » et « TP/SL » sont bien à l'intérieur d'une ligne cliquable), puis que le délégué de clic teste `[data-close]` et `[data-tp]` **avant** `[data-select]` — l'ordre est ce qui fait marcher la chose |
| **Une ouverte reste joignable** | instantané de test avec 70 clôturées **puis** 34 ouvertes : les 34 ouvertes sont présentes malgré le plafond, placées en tête, et le plafond ne rogne que l'historique ; sans position ouverte, le plafond garde son ancien comportement |
| **Aucune route de vente** | 3 échecs « No routes found » d'une règle automatique : compteur écrit, position ouverte, **aucune** liquidation prématurée ; le 4<sup>e</sup> ferme la position, `exit_unpriced`, prix de sortie = dernier prix Dexscreener, produit calculé à ce prix, **une seule** ligne de journal, PnL figé sur un tick ultérieur |
| **Un clic suffit** | même pool vide, mais via `close_position` (le clic) : la **première** demande ferme la position, `liquidation: unpriced`, une seule tentative comptée, et le journal dit « à votre demande » avec **le même prix** que celui écrit sur la position |
| **Vente partielle sans route** | 25 % demandés sur un pool vide : refus explicite, position **ouverte et entière** (15 M de jetons), aucun compteur avancé, message qui dit de fermer la totalité |
| **Panne passagère** | 6 échecs `HTTP 429` d'affilée : compteur **vide**, position ouverte, aucun prix inventé — seul « aucune route » compte ; le message dit que Jupiter n'a pas répondu et de réessayer, au lieu de laisser croire que le marché est vide |
| **Réelle jamais soldée** | même situation sur une position réelle (wallet et solde doublés) : échec franc, position **ouverte**, aucun `exit_unpriced` |
| **La route revient** | deux échecs, puis un devis valide : la vente passe, la position n'est pas marquée, le compteur retombe à 0 |
| **Hermétisme** | `state._write` remplacé par un compteur, `sign_and_send` lève si on l'appelle, `data/settings.json` inchangé |

### Première connexion Telegram

La toute première connexion nécessite un code de confirmation (envoyé dans
Telegram). Ouvrez **Settings → Telegram** dans le dashboard, cliquez sur
**Connecter Telegram**, saisissez le numéro, puis le code reçu. La session est
ensuite enregistrée dans `data/telegram.session` et la connexion devient
automatique au démarrage.

---

## Architecture

```
backend/
  main.py               API FastAPI + WebSocket + frontend statique
  bot.py                Orchestration : file d'analyse série → GMGN → décision → exécution
  telegram_listener.py  Écoute Telegram (Telethon) + login web
  analyzer.py           Moteur de décision (critères de snipe)
  alertfilter.py        Pré-filtre sur le message d'alerte (rejet sans appel GMGN)
  gmgn.py               Client gmgn-cli (gate à priorités, cache, rate-limit)
  jupiter.py            Devis + construction du swap Jupiter (API publique, gratuite)
  solana_rpc.py         RPC Solana : pool de connexions, lot de lectures, WebSocket
                        de confirmation, santé des nœuds, signature locale (solders)
  trader.py             Exécution des ordres + suivi TP/SL/Trailing
  pricefeed.py          Suivi de prix Dexscreener continu (un seul poller, ticks WebSocket)
  dexscreener.py        Prix & PnL temps réel
  clock.py              Calibration de l'horloge (requis par GMGN)
  net.py                TLS via le magasin Windows + un client HTTP mutualisé
                        (connexions réutilisées, secrets jamais journalisés)
  state.py              État persistant (positions, calls, logs, stats)
  secrets.py            Secrets du tableau de bord (~/.config/solsnipbot/secrets.env) :
                        priorité, validation, masquage, tests, jamais de valeur en clair
tools/
  selfcheck.py          Auto-contrôle de bout en bout (aucun ordre envoyé)
  verify_params.py      Chaque paramètre change-t-il vraiment le comportement ?
                        (catalogue, persistance, bornes, filtres, sorties, ordre, pilotage)
  verify_secrets.py     Les clés du tableau de bord : priorité, validations, fichier,
                        masquage, et refus côté API (aucun secret réel écrit)
  verify_mode.py        La bascule papier / réel : les trois états, le fait qu'un clic
                        n'arme jamais le trading réel seul, et le patch construit (rien écrit)
  verify_exits.py       La chaîne de sortie : seuils, contrôle du prix exécutable
                        (le maillon qui annule un TP), boucle complète en papier et en
                        réel, montant exact, invariant de mode (rien signé, rien écrit)
  verify_once.py        Un seul achat par token (fermé compris), refus à l'entrée et à
                        l'achat, pas de sur-blocage par nom, survie au redémarrage, et
                        PnL d'un trade fermé figé (rien signé, rien écrit)
  verify_close.py       Fermeture manuelle : confirmation, parts 25 / 50 / 100, vente
                        partielle qui laisse la position ouverte, vente totale qui fige
                        le PnL, bornes du pourcentage, refus 429 sans dégât, papiers et
                        réels dans les deux sens, clic qui atteint vraiment le code (le
                        bouton vit dans une ligne cliquable), toute position ouverte
                        joignable malgré le plafond de l'instantané, et liquidation au
                        dernier prix quand plus aucune route n'existe (rien signé, rien
                        écrit)
  priority_fee.py       Frais de priorité que Jupiter a réellement demandés, lus sur
                        la chaîne (le plafond écrase-t-il le prix du marché ?)
  verify_clock.py       Correction d'horloge : décalage appliqué à l'horodatage,
                        et ce que `gmgn-cli` lit vraiment dans son propre `.env`
                        (une valeur périmée y prime) — 24 vérifications
frontend/
  index.html / styles.css / app.js   Dashboard
data/                   settings.json, positions.json, calls.json, logs.json, session
```

### Points techniques importants

1. **Calibration de l'horloge** — GMGN valide le timestamp signé à ±5 s. Si
   l'horloge de la machine dérive, toutes les requêtes échouent
   (`AUTH_TIMESTAMP_EXPIRED`). Le bot mesure la dérive au démarrage et corrige
   automatiquement chaque appel. La dérive est visible dans *About*.
   Piège vérifié le 9 novembre 2026 : `gmgn-cli` charge `~/.config/gmgn/.env`
   avec `override: true`, donc un `GMGN_TIME_OFFSET_MS` **périmé** écrit dans ce
   fichier prime sur la valeur du bot. Mesure du jour : fichier à `-93900 ms`
   pour une dérive réelle de `-117000 ms` — 23 s d'écart, et **tous** les appels
   GMGN refusés avec un message qui accuse la clé API. Le bot réécrit donc cette
   ligne quand elle ne correspond plus à sa mesure (`tools/verify_clock.py`,
   24 vérifications).
2. **Anti-rate-limit** — GMGN bannit les IP en cas d'abus. Toutes les requêtes
   passent par une **file unique série** avec espacement minimum, mise en cache
   et **cooldown automatique** en cas de ban. Les analyses sont limitées en
   débit, dédupliquées (30 min) et remises en file si nécessaire.
3. **Aucune donnée simulée** — si un achat échoue (solde insuffisant, etc.), la
   raison réelle est enregistrée. Aucune position fictive n'est créée.
4. **Exécution Jupiter (signature locale)** — le devis et la transaction viennent
   de l'API publique Jupiter (`lite-api.jup.ag`, sans clé), la clé du wallet
   signe **sur la machine**, puis la transaction signée est envoyée au nœud RPC.
   Aucun plan GMGN n'est impliqué, et la clé privée ne sort jamais du poste.
   Points vérifiés avant tout envoi :
   - la transaction est **simulée avant d'être diffusée**. Une route proposée par
     Jupiter peut être inexécutable à cet instant précis : constaté ici, une
     route `Whirlpool > Manifest` refusée par la simulation
     (`InstructionError [6, Custom 1]`) sur le nœud configuré **et** sur un
     second nœud, alors que trois autres routes du même swap passaient. Envoyer
     cette transaction payerait les frais de base et le *priority fee* (jusqu'à
     105 000 lamports, soit 10 % d'un trade de 0,001 SOL) pour un achat qui
     n'aurait pas lieu. Une route refusée est donc remplacée par un **devis
     neuf** (3 tentatives) ; si aucune route ne passe, **rien n'est envoyé** et
     aucun frais n'est payé. Une simulation illisible (nœud muet) ne bloque pas
     le trade : l'envoi direct est tenté, et c'est la chaîne qui tranche ;
   - une panne **passagère** de Jupiter (HTTP 429 *rate limit*, 5xx, coupure
     réseau) est distinguée d'un refus **définitif** (`TOKEN_NOT_TRADABLE`,
     requête invalide). Seule la première est réessayée (3 tentatives, attente
     0,5 s puis 1 s) : un appel perdu est une entrée perdue, et en mode papier
     un remplissage virtuel perdu — donc un test faussé. Un refus définitif
     remonte immédiatement, sans réessai inutile ;
   - le **payeur** de la transaction doit être le wallet local, sinon refus ;
   - la transaction ne doit contenir **qu'une seule signature** attendue ;
   - la signature locale doit être **celle renvoyée par le RPC**, sinon refus ;
   - la quantité de tokens réellement reçue est lue **sur la chaîne**
     (`getTokenAccountsByOwner`), jamais estimée.
5. **Jito** n'est pas utilisé : son *block engine* n'est pas joignable depuis
   cette machine (timeout réseau). La protection anti-MEV repose sur le routage
   Jupiter (qui ne passe pas par un pool exposé) et sur la priorité de frais.
   Le réglage `anti_mev` n'a donc d'effet qu'en moteur `gmgn`.
6. **TLS et antivirus** — Avast (comme d'autres antivirus) ré-émet les
   certificats HTTPS. Windows valide cette chaîne (`curl` passe), Python non, ce
   qui donne un faux « certificate has expired ». Le bot valide donc TLS via
   `truststore`, c'est-à-dire le **magasin de certificats Windows** : la
   vérification reste **active**, exactement comme dans le navigateur. Aucun
   certificat n'est ignoré.
7. **RPC Solana** — réglable dans *Settings → RPC Solana*, avec un **RPC de
   secours** utilisé dès qu'une lecture échoue sur le principal. Un nœud public
   (`api.mainnet-beta.solana.com`) est rate-limité et fait rater des snipes ;
   certains nœuds gratuits échouent par intermittence, voire **définitivement**.
   Le nœud doit répondre à `getGenesisHash` = mainnet-beta, ce que le bot peut
   vérifier.

   Trois règles gouvernent cette couche, chacune payée par un incident réel :

   - **Une diffusion ne se rejoue jamais.** `sendTransaction` reste sur le nœud
     principal, une seule tentative : renvoyer une transaction déjà acceptée
     serait la diffuser deux fois. En revanche `simulateTransaction` **bascule**
     comme une lecture — une simulation ne diffuse rien. Elle avait été exclue du
     repli en même temps que la diffusion : le jour où le nœud principal est
     devenu muet, le contrôle gratuit qui protège un trade d'une route
     inexécutable s'est éteint **sans bruit**, pendant que les lectures
     continuaient de basculer.
   - **Un nœud muet doit se voir.** Un endpoint rate-limité qui meurt répond
     `HTTP 200` avec un **corps vide** : le socket va bien, la donnée non. Chaque
     tentative est donc enregistrée (succès, échecs, latence médiane, dernière
     erreur) et affichée dans le bandeau *Nœud Solana* — plus jamais un primaire
     mort qui laisse le bot boiter en silence.
   - **La clé API ne sort jamais.** Les URL sont affichées masquées
     (`?api-key=***`) ; le contrôle automatique refuse toute sortie contenant la
     clé en clair. Pour ne pas la stocker du tout, mettez
     `https://mainnet.helius-rpc.com/?api-key={api_key}` dans *RPC Solana* et la
     clé dans la variable d'environnement `HELIUS_API_KEY` : la rotation devient
     un changement d'environnement.

   **Helius** est le nœud recommandé (celui configuré par défaut ici). Il apporte
   aussi deux choses qu'aucun nœud public ne donne : le **WebSocket** (voir
   plus bas) et `getPriorityFeeEstimate`, qui indique le prix de priorité du
   marché. Le bandeau *Frais de priorité* affiche les deux nombres **dans la
   bonne unité** : le plafond converti en prix autorisé (µLamports/CU, via le
   budget de CU réellement demandé par les swaps, `DEFAULT_CU_BUDGET`) et
   l'estimation du nœud. Il ne porte **aucun verdict de couleur** — voir
   ci-dessous pourquoi.

   Cette estimation du nœud est **volatile** (317 335, puis 157 899, puis 50 323
   µLamports/CU en quelques minutes) et s'est révélée trop basse d'un ordre de
   grandeur face à ce que Jupiter demande vraiment. Un voyant vert appuyé sur un
   chiffre pareil serait une fausse assurance — pire que pas de voyant. La
   réponse objective vient donc de la chaîne : `tools/priority_fee.py` décode les
   instructions `ComputeBudget` des swaps passés et affiche, pour chacun, le prix
   demandé par Jupiter face à ce que le plafond actuel aurait laissé payer.
8. **Deux prix, deux usages** — le **prix d'entrée** est le prix de remplissage
   réel (SOL dépensés ÷ tokens reçus), le **PnL** est calculé sur les SOL
   réellement encaissés, et la **décision de sortie** est confirmée par un devis
   Jupiter. Le prix Dexscreener ne sert qu'au suivi continu (gratuit, seconde par
   seconde) : sur un token qui vient d'être lancé, il peut être largement faux
   parce que son pool n'est pas encore indexé. Aucune règle ne se déclenche donc
   sur un prix que le marché n'offre pas.
9. **Confirmation par WebSocket** — apprendre qu'un ordre est passé en
   interrogeant `getSignatureStatuses` toutes les secondes, c'est le savoir
   jusqu'à une seconde trop tard ; et un take-profit détecté une seconde trop
   tard sort au mauvais prix. Le bot garde donc une **connexion WebSocket
   permanente** vers le nœud et s'abonne à la signature dès qu'il a envoyé la
   transaction : la réponse arrive quand le nœud la connaît. Le sondage reste en
   place **comme repli** — si le socket tombe, se tait, ou refuse l'abonnement,
   le bot se comporte exactement comme avant. Le WebSocket peut rendre la
   confirmation plus rapide, jamais la faire échouer.

---

## Latence : comment le bot reste rapide

Sur ce type de bot, la rentabilité se joue à la seconde : un token snipé 60 s
après l'alerte a déjà pompé. Le goulot d'étranglement est GMGN (une requête à
la fois, ~1,7 s par appel). Cinq optimisations le retirent du chemin critique.

| # | Optimisation | Gain |
|---|---|---|
| 1 | **Pré-filtre sur le message d'alerte** : les alertes contiennent déjà `Open: 21s ago`, `TOP 10: 72.93%`, `MCP: $3.1K`. Une violation nette d'un plafond est rejetée **sans aucun appel GMGN** | ~13 % des calls traités en 0 ms |
| 2 | **Requête `security` différée** : elle ne sert qu'au contrôle honeypot. Elle n'est demandée que sur un token qui passerait **tout** le reste | 2 appels GMGN → **1** sur ~95 % des calls |
| 3 | **Dexscreener en parallèle** de GMGN (service distinct, aucune limite partagée) | −0,6 s |
| 4 | **Gate à priorités** : un achat (`swap`) ou une analyse passent devant le polling des graphiques du dashboard, qui partageait la même file | l'achat ne fait plus la queue derrière le dashboard |
| 5 | **Espacement mesuré entre les *départs*** de requêtes (et non après la fin) : un appel plus long que le gap n'attend plus | −1,5 s sur les analyses à 2 appels |
| 6 | **Connexions HTTPS mutualisées + lectures en lot** (`net.post_json`, `rpc_batch`) : un handshake TLS neuf coûtait ~310 ms de plus **par appel**, et trois lectures tiennent désormais dans un seul aller-retour | ~610 ms → **~300 ms** par appel ; 3 lectures = **1 aller-retour** |

Et le suivi d'une position ouverte ne bloque plus la file : la lecture du solde
de tokens se fait en tâche de fond.

Le **suivi de prix** ne consomme plus qu'une requête par intervalle pour tout le
monde : positions, token sélectionné, liste de suivi et calls récents sont
demandés **dans une seule requête groupée Dexscreener**, que le gestionnaire de
sorties lit ensuite au lieu de la refaire. Les décisions de TP/SL profitent donc
de prix plus frais **sans** augmenter le débit d'appels.

**Mesuré sur données réelles** (mesures avant/après sur les appels du canal) :

| Étape | Avant | Après |
|---|---|---|
| Analyse d'un call rejeté | **~6,0 s** | **~1,9 s** |
| Requêtes GMGN par call rejeté | 2 | 1 |
| Un token en attente dans la file | 6 s de retard par call devant lui | 1,9 s |
| Requêtes GMGN simultanées | 1 | 1 (inchangé — sécurité anti-ban) |
| Handshake TLS, par appel RPC (même nœud) | ~610 ms | **~300 ms** (connexion réutilisée) |
| Trois lectures RPC | 3 allers-retours (~1800 ms) | **1 aller-retour** (~300 ms) |
| Confirmation d'une transaction | sondage toutes les 1 s (jusqu'à 1000 ms de retard) | **notification WebSocket** (~0,3 s), sondage conservé en repli |

> Le pré-filtre ne peut **que rejeter**, jamais accepter, et seulement sur une
> violation *nette* (avec marge, car l'alerte arrondit ses chiffres). Tout token
> retenu passe par l'analyse GMGN complète, seule source de vérité pour l'achat.
> Il se désactive dans *Settings → Critères de snipe*.

---

## État vérifié en conditions réelles

- Telegram connecté (`@votre_compte`), **3/3 canaux résolus**, écoute continue.
- Calls réels analysés par GMGN (token info + sécurité) et Dexscreener.
- Moteur de critères validé sur des tokens réels : certains passent `9/9`
  (`Snipped`), les autres sont rejetés avec la raison exacte.
- Les tentatives d'achat sont de **vrais ordres** : le bot a bien tenté un achat
  et l'a refusé avec le motif réel `Solde insuffisant: 0.0089 SOL disponible pour
  un achat de 0.5 SOL`.
- Prix SOL et prix des tokens suivis en direct (Dexscreener).
- **Suivi de prix continu vérifié en direct** : un seul flux Dexscreener, une
  requête groupée toutes les 2 s, **0 erreur** sur des centaines de relevés
  (latence mesurée 108–205 ms), poussé par WebSocket — les ticks arrivent dans le
  tableau de bord et le graphique sans rechargement.
- **3 trades réels exécutés** (achat + vente on-chain) : le PnL affiché est
  désormais recalculé sur les **SOL réellement reçus** relus dans la transaction.
- **Journal de bord réel** : 4 positions clôturées, dont un **snipe automatique
  complet** (call Telegram → analyse GMGN 9/9 → achat on-chain → suivi → sortie →
  récupération du loyer) et un token détenu sans position (nœud RPC en échec)
  **adopté puis revendu** par le bot lui-même.
- `tools/selfcheck.py` vérifie en une commande : TLS/RPC, clé, paramètres,
  validation des réglages, filtres, sorties TP/SL/trailing, contrôle du prix
  exécutable, montants de vente et la chaîne d'exécution Jupiter (devis →
  signature locale → **simulation** RPC, aucun ordre envoyé).
- **Mode papier vérifié en conditions réelles** (vrai devis Jupiter, vraie
  simulation, vrai prix de marché) : un achat virtuel a été ouvert puis clos sur
  un token réel — route `Pump.fun`, impact **0,71 %**, **0 transaction émise**,
  solde du wallet **inchangé au lamport** (0,004849152 → 0,004849152 SOL). Les
  frais virtuels suivent exactement le barème du critère « Coût des frais ».
- **Mode papier en service réel** : le mode a été activé à **0,1 SOL** et le bot a
  pris seul deux appels Telegram réels, puis les a clos en virtuel — `DAE`
  (+44,24 % en 75 s) et `PMARCA` (+16,94 % en 190 s), tous deux sur
  `take_profit`, **sans une seule transaction émise**. Les deux remplissages
  portent `route non validée par la simulation` (le solde du wallet ne couvre pas
  une enveloppe de 0,1 SOL) : le prix reste celui du devis réel.
- Une panne **passagère** de Jupiter (HTTP 429, 5xx, coupure réseau) est
  réessayée 3 fois ; un refus **définitif** (`TOKEN_NOT_TRADABLE`) ne l'est
  jamais. Vérifié par 13 cas hors-ligne plus un devis réel en direct.
- **Token invendable traité en direct** : `SOOTS` (pool vidé, `No routes found`
  sur chaque devis) demandait une vente toutes les 5 s — **34 échecs** avant le
  correctif. Après comptage de 4 tentatives, la position papier a été soldée une
  seule fois au dernier prix Dexscreener, marquée `prix non exécutable`
  (`exit_unpriced`, PnL −14,22 %, produit 0,08613247 SOL), badge affiché dans le
  tableau de bord, **zéro requête Jupiter** ensuite pour ce token. Une position
  réelle dans la même situation n'est jamais soldée (prouvé hors-ligne).
- **Fermeture à la main sur un pool vide, mesurée** : `$GTA6` (aucune route
  Jupiter) fermé par **un seul** clic sur « Fermer » — 1,09 s, `exit_unpriced`,
  `exit_price_source: dexscreener`, une tentative comptée, toast « soldée au
  dernier prix connu (non exécutable) », 0 erreur console. Une vente partielle de
  25 % sur le même type de pool répond le refus et **laisse la position entière**
  (2 399 517,817502 jetons avant et après).
- La section **8 « papier »** de l'auto-contrôle prouve, sans réseau, qu'aucun
  ordre ne part (le `sign_and_send` y est remplacé par une fonction qui **échoue
  si elle est appelée**), qu'une position **réelle** est toujours vendue pour de
  vrai même mode papier allumé, qu'une position **papier** est toujours close en
  virtuel même mode papier éteint, et qu'une **vente partielle** ne fausse ni le
  pourcentage de PnL ni le prix de sortie moyen. Ces tests lisent la **taille
  réglée** (0,1 SOL comme 0,002) au lieu de la figer, et comptent le tableau de
  bord papier sur son contenu réel — l'auto-contrôle reste donc valable pendant
  que le mode papier accumule de vrais trades.
- **Couche Solana vérifiée en direct sur Helius** : lectures réelles, **lot de 3
  lectures en un seul aller-retour** (~300 ms, contre ~610 ms par appel avec un
  handshake neuf), WebSocket connecté recevant les slots en continu (~0,3 s
  d'inactivité), **clé API masquée partout**, et surtout le remplacement du nœud
  mort : le RPC configuré auparavant (`leorpc`) répondait `HTTP 200` avec un
  **corps vide sur toutes les méthodes**. Le bot ne tenait que par le repli des
  lectures — **aucun envoi n'aurait abouti**. Vingt-quatre vérifications dédiées
  (section **1b** de `tools/selfcheck.py`) empêchent la récidive silencieuse.
  Le rapport « un lot coûte le prix d'une lecture » se mesure sur **le meilleur de
  trois** de chaque côté, jamais sur une seule paire : un nœud qui répond une fois
  en 1,7 s au lieu de 0,3 s (constaté, sans échec enregistré) faisait passer une
  affirmation vraie au rouge. Le seuil, lui, n'a pas bougé — trois allers-retours
  ne battent jamais un seul, quel que soit l'échantillonnage.
- **Unité des frais de priorité tranchée sur la chaîne** : en décodant les
  instructions `ComputeBudget` de **19 swaps réels** (`tools/priority_fee.py`),
  `prix (µLamports/CU) × limite de CU / 10⁶` égale **exactement** la priorité
  payée — sur chaque ligne, sans exception. C'est ce qui fixe l'unité au lieu de
  la supposer. Les limites demandées par Jupiter tombent entre 83 435 et
  115 665 CU (médiane 110 012), et non 1,2 M : c'est ce budget, mesuré, qui sert
  à convertir le plafond en prix du marché.
- **Le plafond `priority_fee` est mesurablement serré** : sur ces 19 swaps, le
  prix demandé par Jupiter a une médiane de **323 701 µLamports/CU** (intervalle
  24 934 – 878 688), alors que le plafond actuel de 0,00002 SOL n'autorise que
  **181 818 µLamports/CU**. Il écraserait donc **14 swaps sur 19**. La
  transaction aboutit quand même, mais elle est incluse moins vite. Le relever à
  0,0001 SOL porterait la part des frais de 0,055 % à **0,215 %** d'un trade de
  0,1 SOL — le plafond de 3 % n'est pas en cause. C'est un **chiffre, pas une
  décision** : le réglage n'a pas été modifié.

---

## Dépannage

| Problème | Solution |
|---|---|
| `gmgn-cli introuvable` | `npm install -g gmgn-cli` |
| `AUTH_TIMESTAMP_EXPIRED` | Horloge système décalée. Corrigez l'heure Windows si possible ; sinon le bot compense seul la dérive **et** réaligne `GMGN_TIME_OFFSET_MS` dans `~/.config/gmgn/.env`, dont une valeur périmée prime sur la sienne (journalisé au démarrage, voir `tools/verify_clock.py`) |
| `RATE_LIMIT_BANNED` | Attendez la fin du ban (affichée) ; le bot réduit automatiquement son débit |
| Aucun call détecté | Vérifiez **Settings → Telegram** (statut « connecté » et canaux résolus) |
| Achat non exécuté | Vérifiez le solde SOL du wallet (il doit couvrir le montant **+ ~0,003 SOL** de frais/loyer). En moteur `gmgn`, vérifiez aussi que `GMGN_PRIVATE_KEY` est présente |
| `Clé privée du wallet absente` | Créez `~/.config/solsnipbot/wallet.key` (voir *Configuration du wallet d'exécution*) |
| `Clé privée locale = … mais wallet suivi = …` | La clé locale ne contrôle pas le wallet suivi : l'achat est refusé volontairement (aucun fonds ne partirait d'un autre compte). Corrigez la clé ou le wallet |
| `Solde de tokens nul sur la chaîne` | La vente est refusée car le wallet ne détient rien pour ce token — le token a peut-être été vendu ou jamais reçu |
| `Transaction non confirmée après 45s` | Le RPC public est lent/chargé. Configurez un RPC dédié (Helius/QuickNode/Triton) dans *Settings → RPC Solana*. Le bot revérifie l'historique avant d'abandonner, puis relit le solde on-chain : un achat arrivé malgré tout est **toujours** enregistré |
| `certificate has expired` / `SSL: CERTIFICATE_VERIFY_FAILED` | Antivirus qui inspecte le HTTPS (Avast, etc.). Le bot utilise déjà le magasin de certificats Windows (`truststore`) : mettez `pip install truststore` à jour, ou ajoutez une exception HTTPS pour l'application dans l'antivirus |
| Achat manuel non exécuté alors que « Sniper automatique » est off | Comportement corrigé : un snipe **manuel** n'exige plus que l'interrupteur maître `Trading réel activé`. Il obéit en revanche à « un seul achat par token » : un contrat déjà traité est refusé **avant** l'analyse, avec la phrase `déjà traité — un seul achat par token, snipe manuel refusé` |
| `RPC …: Internal JSON-RPC error` sur `getTokenAccountsByOwner` | Certains nœuds publics gratuits ne savent pas indexer les comptes de tokens (et renvoient alors des erreurs **ou des listes vides**). Le bot bascule automatiquement sur le **RPC de secours** (*Settings → RPC de secours*) et le journal signale chaque bascule. Pour du trading réel, utilisez un nœud dédié |
| `RPC …: le nœud ne renvoie rien (200 sans corps)` | Le nœud principal est **mort** (rate-limit atteint, service arrêté) : c'est la signature d'un endpoint qui répond `HTTP 200` avec un corps vide. Les lectures basculent seules sur le secours, et le bandeau *Nœud Solana* passe au rouge — mais **une diffusion ne bascule jamais** (elle serait diffusée deux fois) : tant que le principal ne répond pas, aucun achat ne partira. Changez de nœud dans *Settings → RPC Solana* |
| *Frais de priorité* : l'estimation du nœud est très inférieure à ce que Jupiter demande | C'est normal et sans conséquence pour le bandeau : `getPriorityFeeEstimate` est une **estimation volatile**, qui a déjà annoncé 50 323 µLamports/CU quand les swaps de ce wallet demandaient une médiane de 323 701. Le bot n'en tire volontairement **aucun verdict**. Pour la mesure réelle, lancez `python tools\priority_fee.py` : il lit la chaîne et dit combien de swaps le plafond écrase. Si le chiffre vous gêne, montez `priority_fee` dans *Settings → Critères de snipe* — à 0,1 SOL, passer de 0,00002 à 0,0001 SOL porte la part des frais de 0,055 % à 0,215 %, très loin du plafond de 3 % |
| Le WebSocket est « inactif (sondage utilisé) » | Le bot confirme alors par sondage, exactement comme avant : **aucune perte de fonction**. Vérifiez que le nœud expose bien un WebSocket (`wss://…`) et que l'antivirus ne coupe pas les connexions persistantes |
| Paramètre refusé à l'enregistrement (`400`) | Normal : une valeur vide, négative ou hors bornes est rejetée avec un message explicite plutôt que d'être acceptée (un champ *Stop Loss* vidé désactivait silencieusement le stop) |
| Un token détenu n'apparaît dans aucune position | Le bot **adopte** au démarrage tout token détenu sans position (`Position récupérée: …`) et le remet sous TP/SL. Le PnL d'entrée reste inconnu dans ce cas |
| Achat exécuté mais position absente | Corrigé : un solde de tokens illisible ne transforme plus un achat abouti en « échec » ; la position est complétée en tâche de fond (4 tentatives) |
| Tous les snipes refusés avec `frais estimés … = X % du trade` | Volontaire : à 0,001 SOL par trade et un priority fee de 0,0001 SOL, l'aller-retour coûte ~21 % du trade (mesuré : 2,35 à 15,58 %, médiane 10,88 %). Augmentez le montant par trade (≥ 0,002 SOL avec un priority fee de 0,00002) ou relevez `max_fee_percent` — le panneau *Configuration de Trading* affiche le coût en direct |
| Stop Loss déclenché alors que le token est en fait en hausse | Corrigé : le prix affiché Dexscreener d'un token fraîchement lancé peut être 1,5 à 2× trop bas (pool non indexé, `liquidité $0`). Toute sortie est désormais confirmée par un **devis Jupiter réel** ; le journal indique `Sortie « … » recalculée` (le PnL, lui, a toujours été calculé sur les SOL réellement reçus) |
| Take Profit clôturé en **perte** (ex. `take_profit` à −3,3 % sur ZUCK) | Corrigé : le contraire du cas précédent — le prix affiché avait grimpé **au-dessus** du marché (3,547e-06 affiché pour 2,947e-06 réellement obtenu) et l'ancienne tolérance de ±25 % laissait passer la sortie. Un Take Profit doit maintenant être **exécutable** : si le prix réellement obtenu n'est pas au niveau du Take Profit, la sortie est annulée (`Sortie « take_profit » recalculée … aucune`) et la position continue. Les lignes historiques concernées portent désormais un badge `net négatif` |
| PnL plus faible qu'annoncé | Les frais de transaction (réseau **et** priorité, achat/vente/clôture) sont maintenant **inclus** dans le PnL. Les positions fermées avant ce correctif ont été **recalculées depuis la chaîne** (`tools/backfill_fees.py`) : un `+2,5 %` peut devenir `−2,5 %` une fois les frais comptés |
| PnL mangé par les frais de priorité | Les frais de priorité sont consommés **en totalité** : ~100 000 lamports par achat, soit **10 %** d'un trade de 0,001 SOL (et autant à la vente). Réduisez *Settings → Frais de priorité* ou augmentez le montant par trade pour diluer ce coût fixe |
| `403 PLAN_UPGRADE_REQUIRED` | Uniquement avec le moteur `gmgn`. Passez à un plan **Plus/Pro** (https://gmgn.ai/ai?chain=sol&tab=paid_plans) puis **« J'ai mis à niveau — réessayer »**, **ou** repassez le moteur d'exécution sur `jupiter` (Settings → Moteur d'exécution), qui n'utilise aucun plan GMGN |
| Le suivi affiche `aucun tick` | Dexscreener ne publie pas encore de paire pour ce token (lancement très récent, encore sur la courbe). Aucun prix n'existe côté Dexscreener : le bot l'affiche comme tel et ne fabrique rien. Le token reste demandé à chaque tick jusqu'à son indexation |
| Badge `flux en retard` / `pause N s` | La requête Dexscreener a échoué : le bot élargit automatiquement l'intervalle (jusqu'à 30 s) puis revient au rythme normal, et le journal indique l'erreur exacte. Les derniers prix connus gardent leur âge affiché. Les sorties TP/SL basculent alors sur une lecture directe, donc la protection continue |
| Le graphique ne bouge pas alors que le prix du panneau bouge | Normal si la pastille `graphique ×N` est présente : le prix Dexscreener s'écarte trop du dernier chandelle GMGN pour être dessiné sans inventer une mèche. Le chandelier se recale au prochain rafraîchissement (20 s) |
| Le prix ne bouge plus du tout dans *Positions Ouvertes* | Vérifiez *Settings → Suivi de prix continu* (activé) et *Intervalle du suivi en direct*. Le gestionnaire de sorties ne lit plus Dexscreener lui-même : il consomme le flux et ne fait une lecture directe que si le tick est trop vieux |
| Trop de requêtes Dexscreener (tokens suivis nombreux) | Le flux est plafonné à **30 tokens** (une requête groupée de 30 adresses). Réduisez *Calls récents suivis en direct* (0 pour ne suivre que les positions, la sélection et la liste) ou augmentez *Intervalle du suivi en direct* |
| Le bot n'envoie plus aucun ordre alors que « Trading réel » est activé | Le **mode papier** est actif (`Settings → Mode papier`). Il remplace le trading réel : `live_trading` ne décide plus rien tant qu'il est allumé. L'en-tête affiche « Mode papier — aucun ordre réel » |
| Un achat est refusé en mode papier (`aucune route exécutable`) | Volontaire : la route Jupiter a été **refusée par la simulation**, donc un ordre réel aurait payé ses frais pour rien. Le journal indique le message exact de la chaîne |
| Une vente papier échoue (`Aucune route de sortie`) | Le token n'a plus aucune route de vente chez Jupiter : il est réellement invendable. **Un clic sur « Fermer » (totalité) suffit** : la position **papier** est soldée une fois au dernier prix Dexscreener et porte le badge `prix non exécutable` — ce prix n'était pas exécutable, ce n'est pas une vente. Une règle **automatique**, elle, attend 4 tentatives comptées. Une vente **partielle** est refusée avec la consigne. Une position **réelle** reste ouverte et continue d'échouer franchement : elle n'est jamais soldée à un prix inventé |
| Mention `route non validée par la simulation` sur une position papier | Le solde du wallet ne couvre pas l'enveloppe WSOL + compte de token de l'achat (celle du montant réglé, plus les frais) : le prix et la route sont réels, mais l'exécution n'a pas pu être confirmée. Aucun SOL n'est engagé : c'est un marqueur d'honnêteté, pas une erreur |
| Mes trades papier n'apparaissent pas dans « Trades » / « Total PnL » | Volontaire : les résultats papier sont **séparés**. Ils sont dans l'encart « Résultats du mode papier » (et `stats.paper` côté API) ; l'en-tête ne compte que les trades réels |
| Le PnL papier est légèrement pire que le prix affiché | Normal : les frais sont comptés en **borne haute** (`base ×3 + priority ×2`, exactement le barème du critère « Coût des frais »). Un ordre réel consomme rarement tout le priority fee, donc le résultat réel sera égal ou un peu meilleur |

---

## Avertissement

Le trading de memecoins Solana est **extrêmement risqué**. Ce bot exécute des
ordres **réels** avec de l'argent **réel**. Testez d'abord avec un montant
minimal (`Montant par trade`), ou désactivez *Sniper automatique* pour
n'analyser qu'en lecture seule. Vous êtes seul responsable de vos pertes.
