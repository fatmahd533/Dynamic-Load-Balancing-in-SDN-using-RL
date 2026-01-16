#!/bin/bash

echo "🔧 CORRECTION ET TEST SDN"

# Nettoyer
sudo pkill -f ryu-manager
sudo mn -c

# Chemin vers ryu-manager
RYU_PATH="$HOME/.local/share/Trash/files/ryu/bin/ryu-manager"

# Corriger le shebang si nécessaire
if [ -f "$RYU_PATH" ]; then
    echo "✅ Correction du script ryu-manager..."
    sed -i 's|#!/usr/bin/env python|#!/usr/bin/env python3|' "$RYU_PATH"
    chmod +x "$RYU_PATH"
else
    echo "❌ ryu-manager non trouvé dans la corbeille"
    echo "Installation via pipx..."
    sudo apt install -y pipx
    pipx ensurepath
    pipx install ryu
    pipx inject ryu networkx
    RYU_PATH="ryu-manager"
fi

# Vérifier NetworkX
if ! python3 -c "import networkx" 2>/dev/null; then
    echo "Installation de NetworkX..."
    pip3 install --user networkx
fi

echo "🚀 Démarrage du contrôleur Q-learning..."
$RYU_PATH controller/ryu_qlearning_lb.py --ofp-tcp-listen-port 6633 > ryu.log 2>&1 &
RYU_PID=$!
sleep 5

if ps -p $RYU_PID > /dev/null; then
    echo "✅ Contrôleur actif (PID: $RYU_PID)"
    echo ""
    echo "📟 Mininet va démarrer..."
    echo ""
    echo "Commandes à tester dans Mininet:"
    echo "  pingall      # Test de connectivité complète"
    echo "  h1 ping -c 3 h3  # Test de chemin spécifique"
    echo "  net          # Voir la topologie"
    echo "  exit         # Quitter"
    echo ""
    echo "📊 Les logs du contrôleur sont dans: ryu.log"
    echo ""
    
    # Démarrer Mininet
    sudo python3 topo.py
    
    # Nettoyer
    kill $RYU_PID 2>/dev/null
else
    echo "❌ Le contrôleur a échoué à démarrer"
    echo "Logs d'erreur:"
    cat ryu.log
fi

sudo mn -c 2>/dev/null
echo "🎉 Test terminé!"
