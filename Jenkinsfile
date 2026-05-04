pipeline {
  agent any

  environment {
    APP_NAME = 'sms-chatgpt'
    NAMESPACE = 'sms-chatgpt'
    REGISTRY_HOST = 'ghcr.io'
    IMAGE_REPOSITORY = 'ghcr.io/YOUR_GITHUB_ORG_OR_USER'
    DOCKER_CREDENTIALS_ID = 'docker-registry-credentials'
    KUBECONFIG_CREDENTIALS_ID = 'kubeconfig'
    OPENAI_API_KEY_CREDENTIALS_ID = 'openai-api-key'
    IMAGE_TAG = "${env.BUILD_NUMBER}"
    DAEMON_IMAGE = "${env.IMAGE_REPOSITORY}/${env.APP_NAME}-daemon:${env.IMAGE_TAG}"
    WORKER_IMAGE = "${env.IMAGE_REPOSITORY}/${env.APP_NAME}-worker:${env.IMAGE_TAG}"
  }

  stages {
    stage('Test') {
      steps {
        sh 'python3 -m unittest discover -s tests'
      }
    }

    stage('Build Images') {
      steps {
        sh 'docker build -f Dockerfile.daemon -t "$DAEMON_IMAGE" .'
        sh 'docker build -f Dockerfile -t "$WORKER_IMAGE" .'
      }
    }

    stage('Push Images') {
      steps {
        withCredentials([usernamePassword(
          credentialsId: env.DOCKER_CREDENTIALS_ID,
          usernameVariable: 'DOCKER_USERNAME',
          passwordVariable: 'DOCKER_PASSWORD'
        )]) {
          sh 'printf "%s" "$DOCKER_PASSWORD" | docker login "$REGISTRY_HOST" -u "$DOCKER_USERNAME" --password-stdin'
          sh 'docker push "$DAEMON_IMAGE"'
          sh 'docker push "$WORKER_IMAGE"'
        }
      }
    }

    stage('Deploy') {
      steps {
        withCredentials([
          file(credentialsId: env.KUBECONFIG_CREDENTIALS_ID, variable: 'KUBECONFIG'),
          string(credentialsId: env.OPENAI_API_KEY_CREDENTIALS_ID, variable: 'OPENAI_API_KEY')
        ]) {
          sh 'kubectl apply -f k8s/namespace.yaml'
          sh 'kubectl -n "$NAMESPACE" create secret generic sms-chatgpt-secrets --from-literal=OPENAI_API_KEY="$OPENAI_API_KEY" --dry-run=client -o yaml | kubectl apply -f -'
          sh 'kubectl apply -f k8s/rbac.yaml'
          sh 'kubectl apply -f k8s/configmap.yaml'
          sh 'kubectl apply -f k8s/deployment.yaml'
          sh 'kubectl -n "$NAMESPACE" set image deployment/sms-chatgpt-daemon daemon="$DAEMON_IMAGE"'
          sh 'kubectl -n "$NAMESPACE" set env deployment/sms-chatgpt-daemon CHAT_POD_IMAGE="$WORKER_IMAGE"'
          sh 'kubectl -n "$NAMESPACE" rollout status deployment/sms-chatgpt-daemon --timeout=120s'
        }
      }
    }
  }

  post {
    always {
      sh 'docker logout "$REGISTRY_HOST" || true'
    }
  }
}
