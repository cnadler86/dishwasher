#!/bin/bash
#
# Dishwasher Controller - Uninstallation Script
# Usage: sudo ./uninstall.sh
#

set -e  # Exit on error

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Service name
SERVICE_NAME="dishwasher-controller"
SERVICE_FILE="${SERVICE_NAME}.service"

echo "======================================================================="
echo "  Dishwasher Controller - Uninstallation Script"
echo "======================================================================="
echo ""

# Check if running as root
if [ "$EUID" -ne 0 ]; then 
    echo -e "${RED}Error: This script must be run with sudo${NC}"
    echo "Usage: sudo ./uninstall.sh"
    exit 1
fi

# Check if service exists
if [ ! -f "/etc/systemd/system/$SERVICE_FILE" ]; then
    echo -e "${YELLOW}⚠${NC} Service is not installed"
    echo "Nothing to uninstall"
    exit 0
fi

echo "This will:"
echo "  - Stop the $SERVICE_NAME service"
echo "  - Disable the $SERVICE_NAME service"
echo "  - Remove the service file from /etc/systemd/system/"
echo ""
echo -e "${YELLOW}Note: This will NOT remove:${NC}"
echo "  - Application files"
echo "  - Log files"
echo "  - Configuration files"
echo ""

# Ask for confirmation
read -p "Continue with uninstallation? (y/N) " -n 1 -r
echo
if [[ ! $REPLY =~ ^[Yy]$ ]]; then
    echo "Uninstallation cancelled"
    exit 0
fi

echo ""
echo "======================================================================="
echo "  Removing Service"
echo "======================================================================="

# Stop service if running
if systemctl is-active --quiet "$SERVICE_NAME"; then
    systemctl stop "$SERVICE_NAME"
    echo -e "${GREEN}✓${NC} Stopped $SERVICE_NAME service"
else
    echo -e "${YELLOW}⚠${NC} Service was not running"
fi

# Disable service if enabled
if systemctl is-enabled --quiet "$SERVICE_NAME" 2>/dev/null; then
    systemctl disable "$SERVICE_NAME"
    echo -e "${GREEN}✓${NC} Disabled $SERVICE_NAME service"
else
    echo -e "${YELLOW}⚠${NC} Service was not enabled"
fi

# Remove service file
rm "/etc/systemd/system/$SERVICE_FILE"
echo -e "${GREEN}✓${NC} Removed service file"

# Reload systemd
systemctl daemon-reload
echo -e "${GREEN}✓${NC} Reloaded systemd daemon"

# Reset failed state if any
systemctl reset-failed "$SERVICE_NAME" 2>/dev/null || true

echo ""
echo "======================================================================="
echo -e "  ${GREEN}Uninstallation Complete${NC}"
echo "======================================================================="
echo ""
echo "The service has been removed from systemd."
echo ""
echo "To completely remove all application files, run:"
echo "  rm -rf $(pwd)"
echo ""
