#!/usr/bin/env python3
import os
import sys

def setup_service():
    # Get the absolute path of the current directory
    current_dir = os.path.abspath(os.path.dirname(__file__))
    
    # Read the service file template
    service_file = os.path.join(current_dir, 'dishwasher.service')
    with open(service_file, 'r') as f:
        content = f.read()
    
    # Replace placeholders with actual paths
    updated_content = content.replace('{WORKING_DIR}', current_dir)
    
    # Write back the updated content
    with open(service_file, 'w') as f:
        f.write(updated_content)
    
    print(f"Service file updated with working directory: {current_dir}")
    print("You can now copy the service file to /etc/systemd/system/ and enable it")
    print("sudo cp dishwasher.service /etc/systemd/system/")
    print("sudo systemctl daemon-reload")
    print("sudo systemctl enable dishwasher.service")
    print("sudo systemctl start dishwasher.service")

if __name__ == '__main__':
    setup_service()
