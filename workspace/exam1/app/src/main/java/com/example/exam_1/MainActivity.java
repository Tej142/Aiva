package com.example.exam_1;

import android.os.Bundle;

import androidx.activity.EdgeToEdge;
import androidx.appcompat.app.AppCompatActivity;
import androidx.core.graphics.Insets;
import androidx.core.view.ViewCompat;
import androidx.core.view.WindowInsetsCompat;
import android.view.View;
import android.widget.*;
import org.w3c.dom.Text;

public class MainActivity extends AppCompatActivity {
    long fact = 1,i;
    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        setContentView(R.layout.activity_main);

    }
    public void fun(View v){
        TextView tv = findViewById(R.id.tv);
        Button bt = findViewById(R.id.bt);
        EditText et = findViewById(R.id.et);
        String t = et.getText().toString();
        int n = Integer.parseInt(t);
        for(i=1;i<=n;i++){
            fact = fact*i;
        }


        Toast.makeText(this,"Factorial:"+fact,Toast.LENGTH_LONG).show();
    }
}